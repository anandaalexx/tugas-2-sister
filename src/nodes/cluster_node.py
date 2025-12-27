import asyncio
import logging
import os
import time
from src.nodes.base_node import BaseNode

logger = logging.getLogger(__name__)

class ClusterNode(BaseNode):
    def __init__(self, node_id: str, config):
        super().__init__(node_id, config)
        self.last_seen = {}
        self.leader_id = None
        self._monitor_task = None
        self.locks = {}
        self.waits = {}  # NEW: untuk mencatat siapa menunggu siapa

    async def start(self):
        logger.info(f"[{self.node_id}] ClusterNode starting...")
        await self.redis.connect()
        await self.redis.subscribe("cluster:events", self._on_cluster_event)
        await self.announce_presence()

        self.running = True
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        self._monitor_task = asyncio.create_task(self._monitor_cluster())

        # NEW: jalankan deteksi deadlock di leader
        self._deadlock_task = asyncio.create_task(self._deadlock_detection_loop())

        try:
            while self.running:
                await asyncio.sleep(0.1)
        finally:
            await self.stop()

    async def _apply_command(self, cmd: str):
        parts = cmd.split(":")
        action = parts[0]

        if action == "LOCK":
            _, resource, owner, mode = parts
            if resource not in self.locks:
                self.locks[resource] = {"owners": set(), "mode": None}

            current_mode = self.locks[resource]["mode"]
            owners = self.locks[resource]["owners"]

            if current_mode is None:
                self.locks[resource]["mode"] = mode
                owners.add(owner)
                self.waits.pop(owner, None)
                logger.info(f"[{self.node_id}] LOCK {resource} by {owner} ({mode})")

            elif current_mode == "S" and mode == "S":
                owners.add(owner)
                self.waits.pop(owner, None)
                logger.info(f"[{self.node_id}] LOCK {resource} shared by {owner}")

            else:
                # conflict → catat siapa menunggu siapa
                if owners:
                    self.waits[owner] = list(owners)
                logger.info(f"[{self.node_id}] LOCK {resource} denied for {owner} - waiting on {owners}")

                # update ke Redis untuk keperluan deteksi deadlock
                await self.redis.sadd(f"waits:{owner}", *owners)

        elif action == "UNLOCK":
            if len(parts) == 3:
                _, resource, owner = parts
            elif len(parts) >= 4:
                _, resource, owner = parts[1:4]
            else:
                return

            if resource in self.locks and owner in self.locks[resource]["owners"]:
                self.locks[resource]["owners"].remove(owner)
                logger.info(f"[{self.node_id}] UNLOCK {resource} by {owner}")
                if not self.locks[resource]["owners"]:
                    self.locks[resource]["mode"] = None

                # hapus dari daftar tunggu
                self.waits.pop(owner, None)
                await self.redis.hdel("waits", owner)

    async def _on_cluster_event(self, channel: str, message: str):
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
            logger.info(f"[{self.node_id}] Received command from leader: {cmd}")
            await self._apply_command(cmd)
            await self.redis.publish("cluster:events", f"ACK:{cmd}:{self.node_id}")

    async def _deadlock_detection_loop(self):
        """Hanya leader yang melakukan deteksi deadlock."""
        while True:
            await asyncio.sleep(10)
            if self.leader_id == self.node_id:
                keys = await self.redis.keys("waits:*")
                graph = {}
                for k in keys:
                    waiter = k.split(":", 1)[1]
                    members = await self.redis.smembers(k)
                    graph[waiter] = list(members)

                # deteksi siklus
                if self._has_cycle(graph):
                    logger.warning(f"[{self.node_id}] ⚠️ Deadlock detected! {graph}")
                else:
                    logger.debug(f"[{self.node_id}] No deadlock detected")

    def _has_cycle(self, graph):
        visited = set()
        stack = set()

        def visit(node):
            if node not in graph:
                return False
            if node in stack:
                return True
            stack.add(node)
            for nei in graph[node]:
                if visit(nei):
                    return True
            stack.remove(node)
            visited.add(node)
            return False

        return any(visit(n) for n in graph)

    async def _heartbeat_loop(self):
        self._monitor_task = asyncio.create_task(self._monitor_cluster())
        await super()._heartbeat_loop()

    async def _monitor_cluster(self):
        election_interval = self.config.get("election_interval", 10)
        heartbeat_timeout = self.config.get("heartbeat_timeout", 15)

        while True:
            await asyncio.sleep(election_interval)
            now = time.time()

            active_nodes = [
                n for n, t in self.last_seen.items() if now - t < heartbeat_timeout
            ]

            if self.leader_id and self.leader_id not in active_nodes:
                logger.warning(f"Leader {self.leader_id} missing! Triggering election.")
                await self._elect_leader(active_nodes)

            elif not self.leader_id:
                await self._elect_leader(active_nodes)

    async def _elect_leader(self, active_nodes):
        candidates = active_nodes + [self.node_id]
        new_leader = sorted(candidates)[0]
        self.leader_id = new_leader
        msg = f"LEADER_ELECTED:{new_leader}"
        logger.info(f"{self.node_id} elected new leader: {new_leader}")
        await self.redis.publish("cluster:events", msg)
        await self.redis.set("current_leader", new_leader)


async def _run_from_env():
    node_id = os.getenv("NODE_ID", "node-unknown")
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    config = {
        "redis_url": redis_url,
        "heartbeat_interval": 5,
        "election_interval": 10,
        "heartbeat_timeout": 15,
    }

    node = ClusterNode(node_id=node_id, config=config)
    await node.start()

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    import asyncio
    asyncio.run(_run_from_env())
