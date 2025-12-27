import logging
from src.nodes.base_node import BaseNode

logger = logging.getLogger(__name__)

class FollowerNode(BaseNode):
    async def _on_cluster_event(self, channel: str, message: str):
        await super()._on_cluster_event(channel, message)

        # Jika menerima command dari leader
        if message.startswith("CMD:"):
            cmd = message[4:]
            await self.apply_command(cmd)
            await self.redis.publish("cluster:events", f"ACK:{cmd}:{self.node_id}")
