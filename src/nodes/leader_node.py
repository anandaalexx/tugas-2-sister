import asyncio
import logging
from src.nodes.base_node import BaseNode

logger = logging.getLogger(__name__)

class LeaderNode(BaseNode):
    def __init__(self, node_id, config):
        super().__init__(node_id, config)
        self.pending_acks = {}

    async def replicate_command(self, command: str):
        """
        Kirim command ke seluruh cluster untuk direplikasi.
        """
        msg = f"CMD:{command}"
        self.pending_acks[command] = set()
        await self.redis.publish("cluster:events", msg)
        logger.info("[%s] Sent command to cluster: %s", self.node_id, command)

        # Tunggu ACK dari follower
        try:
            logger.info("[%s] Waiting ACKs for command: %s", self.node_id, command)
            await asyncio.wait_for(self._wait_for_acks(command), timeout=5)
            logger.info("[%s] Command committed: %s", self.node_id, command)
        except asyncio.TimeoutError:
            logger.warning("[%s] Timeout waiting ACK for %s", self.node_id, command)

    async def _on_cluster_event(self, channel: str, message: str):
        await super()._on_cluster_event(channel, message)

        # Jika menerima ACK
        if message.startswith("ACK:"):
            _, cmd, node = message.split(":", 2)
            if cmd in self.pending_acks:
                self.pending_acks[cmd].add(node)

        # Jika menerima command dari dirinya sendiri
        elif message.startswith("CMD:"):
            cmd = message[4:]
            await self.apply_command(cmd)
            # Publish ACK ke leader
            await self.redis.publish("cluster:events", f"ACK:{cmd}:{self.node_id}")

    async def _wait_for_acks(self, cmd: str):
        while True:
            # Misalnya cluster punya 3 node → butuh minimal 2 ACK
            if len(self.pending_acks[cmd]) >= 2:
                return
            await asyncio.sleep(0.2)
