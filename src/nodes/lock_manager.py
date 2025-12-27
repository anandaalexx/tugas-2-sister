# src/nodes/lock_manager.py
import asyncio
import logging
import time
from .base_node import BaseNode

logger = logging.getLogger(__name__)

class LockManager(BaseNode):
    def __init__(self, node_id: str, config):
        super().__init__(node_id, config)
        self.locks = {}  # resource -> {"owners": set(), "mode": "S"/"X"/None}
        self.pending_acks = {}  # command -> set of node_ids (for replication)
        self.leader_id = None
        self.last_seen = {}  # heartbeat tracker
        self._monitor_task = None
        self._deadlock_task = None

    async def start(self):
        logger.info(f"[{self.node_id}] LockManager starting...")
        await self.redis.connect()
        await self.redis.subscribe("cluster:events", self._on_cluster_event)
        await self.announce_presence()

        self.running = True
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        self._monitor_task = asyncio.create_task(self._monitor_cluster())
        # deadlock detection runs periodically but only does work on leader
        self._deadlock_task = asyncio.create_task(self._deadlock_loop())

        try:
            while self.running:
                await asyncio.sleep(0.1)
        finally:
            await self.stop()

    async def stop(self):
        # cancel tasks from base + this
        self.running = False
        if self._deadlock_task:
            self._deadlock_task.cancel()
            try:
                await self._deadlock_task
            except asyncio.CancelledError:
                pass
            self._deadlock_task = None
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        await super().stop()

    # ---------- helper Redis wait/pending bookkeeping ----------
    async def _record_wait(self, waiter: str, holders: set):
        """Record that `waiter` waits for each holder (Redis set waits:<waiter>)."""
        if not holders:
            return
        key = f"waits:{waiter}"
        # add all holders
        await self.redis.sadd(key, *list(holders))
        # also write pending request so detector can know what resource was requested
        # pending:<waiter> = resource:mode (set by caller)
        logger.debug(f"[{self.node_id}] Recorded wait {waiter} -> {holders}")

    async def _clear_wait(self, waiter: str):
        key = f"waits:{waiter}"
        await self.redis.delete(key)(key)
        await self.redis.delete(f"pending:{waiter}")
        logger.debug(f"[{self.node_id}] Cleared waits for {waiter}")

    async def _get_all_waits(self):
        """Return dict waiter -> set(holders)."""
        ret = {}
        keys = await self.redis._conn.keys("waits:*")
        for k in keys:
            waiter = k.split(":", 1)[1]
            members = await self.redis._conn.smembers(k)
            ret[waiter] = set(members)
        return ret

    # ---------- deadlock detection ----------
    async def _detect_deadlock(self):
        """
        Build wait-for graph from waits:* and find a cycle.
        Return cycle list if found (like [n1, n2, n3, n1]) else None.
        """
        graph = await self._get_all_waits()
        # graph: node -> set(nodes it waits for)
        visited = set()
        stack = set()

        def dfs(node, path):
            visited.add(node)
            stack.add(node)
            for nbr in graph.get(node, []):
                if nbr not in visited:
                    res = dfs(nbr, path + [nbr])
                    if res:
                        return res
                elif nbr in stack:
                    # found cycle — extract cycle path
                    # find position of nbr in path
                    try:
                        idx = path.index(nbr)
                        cycle = path[idx:] + [nbr]
                    except ValueError:
                        cycle = [nbr, nbr]
                    return cycle
            stack.remove(node)
            return None

        for node in list(graph.keys()):
            if node not in visited:
                cycle = dfs(node, [node])
                if cycle:
                    return cycle
        return None

    async def _break_deadlock(self, cycle):
        """Choose a victim and abort its pending request(s)."""
        # simple victim selection: choose node with highest lexicographic id (deterministic)
        victim = sorted(set(cycle))[-1]
        logger.warning(f"[{self.node_id}] Deadlock detected in cycle {cycle}. Choosing victim {victim} to abort.")
        # remove waits and pending associated with victim
        await self.redis._conn.delete(f"waits:{victim}")
        await self.redis._conn.delete(f"pending:{victim}")
        # notify cluster to abort (nodes can cleanup local state on receiving ABORT)
        await self.redis.publish("cluster:events", f"CMD:ABORT:{victim}")
        logger.info(f"[{self.node_id}] Published ABORT for {victim}")

    async def _deadlock_loop(self):
        """Periodically, if I'm leader, check for deadlocks."""
        interval = self.config.get("deadlock_check_interval", 3)
        try:
            while True:
                await asyncio.sleep(interval)
                # only leader runs detection
                if self.leader_id != self.node_id:
                    continue
                cycle = await self._detect_deadlock()
                if cycle:
                    await self._break_deadlock(cycle)
        except asyncio.CancelledError:
            logger.info("Deadlock detection loop cancelled")

    # ---------- main command application ----------
    async def _apply_command(self, cmd: str):
        """
        Commands formats supported:
          LOCK:<resource>:<owner>:<mode>   (mode: S/X, default X)
          UNLOCK:<resource>:<owner>
          ABORT:<owner>
          SET key=value
        """
        parts = cmd.split(":")
        action = parts[0]

        if action == "LOCK":
            # allow both 3 or 4 parts
            if len(parts) == 4:
                _, resource, owner, mode = parts
            elif len(parts) == 3:
                _, resource, owner = parts
                mode = "X"
            else:
                logger.warning(f"[{self.node_id}] Bad LOCK command: {cmd}")
                return

            # ensure structure
            r = self.locks.setdefault(resource, {"owners": set(), "mode": None})
            current_mode = r["mode"]
            owners = r["owners"]

            if current_mode is None:
                # grant lock
                r["mode"] = mode
                owners.add(owner)
                # clear any pending/waits for this owner (if it previously waited)
                await self._clear_wait(owner)
                await self.redis._conn.set(f"held:{resource}", owner)  # optional helper
                
                # Save lock to Redis for API endpoint
                import json
                lock_data = {"owners": list(owners), "mode": mode}
                await self.redis.set(f"lock:{resource}", json.dumps(lock_data))
                
                logger.info(f"[{self.node_id}] LOCK {resource} by {owner} ({mode})")
            elif current_mode == "S" and mode == "S":
                # shared sharing allowed
                owners.add(owner)
                await self._clear_wait(owner)
                
                # Update lock in Redis
                import json
                lock_data = {"owners": list(owners), "mode": "S"}
                await self.redis.set(f"lock:{resource}", json.dumps(lock_data))
                
                logger.info(f"[{self.node_id}] LOCK {resource} shared by {owner}")
            else:
                # conflict -> record wait and pending in Redis
                logger.info(f"[{self.node_id}] LOCK {resource} denied for {owner} ({mode}) - conflict with {r}")
                # record pending: pending:<owner> = resource:mode
                await self.redis._conn.set(f"pending:{owner}", f"{resource}:{mode}")
                # record wait edges: waiter(owner) -> all current owners
                await self._record_wait(owner, set(owners))

        elif action == "UNLOCK":
            # UNLOCK:<resource>:<owner>
            if len(parts) >= 3:
                _, resource, owner = parts[:3]
            else:
                logger.warning(f"[{self.node_id}] Bad UNLOCK command: {cmd}")
                return

            if resource in self.locks and owner in self.locks[resource]["owners"]:
                self.locks[resource]["owners"].remove(owner)
                logger.info(f"[{self.node_id}] UNLOCK {resource} by {owner}")
                # if no owners left, clear mode
                if not self.locks[resource]["owners"]:
                    self.locks[resource]["mode"] = None
                    # also remove helper held:resource
                    try:
                        await self.redis._conn.delete(f"held:{resource}")
                        # Also delete from lock:resource
                        await self.redis.delete(f"lock:{resource}")
                    except Exception:
                        pass
                else:
                    # Update lock in Redis with remaining owners
                    import json
                    lock_data = {"owners": list(self.locks[resource]["owners"]), "mode": self.locks[resource]["mode"]}
                    await self.redis.set(f"lock:{resource}", json.dumps(lock_data))
                
                # clear any wait record for this owner (it might have been waiting earlier)
                await self._clear_wait(owner)
            else:
                logger.warning(f"[{self.node_id}] UNLOCK {resource} failed - owner mismatch or not locked")

        elif action == "ABORT":
            # ABORT:<owner> -> leader requested this owner to cancel pending requests
            if len(parts) >= 2:
                _, victim = parts[:2]
                await self._clear_wait(victim)
                # also inform local structures (if owner maintained local queue)
                logger.info(f"[{self.node_id}] ABORT received for {victim} - cleared pending requests")
            else:
                logger.warning(f"[{self.node_id}] Bad ABORT command: {cmd}")

        elif action == "SET":
            key_value = cmd[4:]
            if "=" in key_value:
                key, value = key_value.split("=", 1)
                await self.redis._conn.set(key.strip(), value.strip())
                logger.info(f"[{self.node_id}] SET {key} = {value}")
            else:
                logger.warning(f"[{self.node_id}] Bad SET command: {cmd}")

        else:
            logger.warning(f"[{self.node_id}] Unknown command: {cmd}")


    # ---------- replication / ack / pubsub handling ----------
    async def replicate_command(self, command: str):
        """Leader: send command to cluster and wait ACKs"""
        msg = f"CMD:{command}"
        self.pending_acks[command] = set()
        await self.redis.publish("cluster:events", msg)
        logger.info(f"[{self.node_id}] Sent command to cluster: {command}")

        try:
            await asyncio.wait_for(self._wait_for_acks(command), timeout=5)
            logger.info(f"[{self.node_id}] Command committed: {command}")
        except asyncio.TimeoutError:
            logger.warning(f"[{self.node_id}] Timeout waiting ACK for {command}")

    async def _wait_for_acks(self, cmd: str):
        """Wait until majority of nodes ack the command"""
        while True:
            if len(self.pending_acks.get(cmd, set())) >= 2:  # minimal 3 nodes
                return
            await asyncio.sleep(0.2)

    async def _on_cluster_event(self, channel: str, message: str):
        """Handle cluster messages"""
        await super()._on_cluster_event(channel, message)

        if message.startswith("HEARTBEAT:"):
            sender = message.split(":")[1]
            self.last_seen[sender] = time.time()

        elif message.startswith("LEADER_ELECTED:"):
            new_leader = message.split(":")[1]
            if new_leader != self.leader_id:
                self.leader_id = new_leader
                logger.info(f"{self.node_id} updated leader to {self.leader_id}")

        elif message.startswith("CMD:"):
            cmd = message[4:]
            await self._apply_command(cmd)
            # always ACK so leader can count replication (ACK contains original cmd and the acking node)
            await self.redis.publish("cluster:events", f"ACK:{cmd}:{self.node_id}")

        elif message.startswith("ACK:"):
            # format ACK:<original_cmd>:<node>
            try:
                _, cmd, node = message.split(":", 2)
                if cmd in self.pending_acks:
                    self.pending_acks[cmd].add(node)
            except ValueError:
                logger.debug(f"[{self.node_id}] Malformed ACK message: {message}")
