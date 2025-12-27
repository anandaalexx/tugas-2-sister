# src/nodes/base_node.py
import asyncio
import logging
import os
from typing import Dict, Any

from src.utils.redis_client import RedisClient

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

class BaseNode:
    def __init__(self, node_id: str, config: Dict[str, Any]):
        self.node_id = node_id
        self.config = config
        self.running = False
        self.redis = RedisClient(url=config.get("redis_url"))

        # heartbeat interval (detik)
        self._hb_interval = config.get("heartbeat_interval", 5)
        self._hb_task = None

    async def start(self):
        logger.info(f"Starting node {self.node_id}")
        await self.redis.connect()

        # subscribe to cluster events channel
        await self.redis.subscribe("cluster:events", self._on_cluster_event)

        # publish initial presence
        await self._publish_presence()

        self.running = True
        # start heartbeat & keep-alive
        self._hb_task = asyncio.create_task(self._heartbeat_loop())

        # keep alive until stopped
        try:
            while self.running:
                await asyncio.sleep(0.1)
        finally:
            # cleanup if exit loop
            await self.stop()

    async def announce_presence(self):
        """Kirim pesan bahwa node ini aktif di cluster."""
        if hasattr(self, "redis"):
            msg = f"HEARTBEAT:{self.node_id}"
            await self.redis.publish("cluster:events", msg)
            logger.info(f"[{self.node_id}] Announced presence to cluster.")


    async def stop(self):
        logger.info(f"Stopping node {self.node_id}")
        self.running = False
        if self._hb_task:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except asyncio.CancelledError:
                pass
            self._hb_task = None
        try:
            await self.redis.unsubscribe("cluster:events")
        except Exception:
            pass
        try:
            await self.redis.close()
        except Exception:
            pass

    async def _publish_presence(self):
        msg = f"NODE_PRESENCE:{self.node_id}"
        logger.info("Publishing presence: %s", msg)
        try:
            await self.redis.publish("cluster:events", msg)
        except Exception as e:
            logger.exception("Failed to publish presence: %s", e)

    async def _heartbeat_loop(self):
        try:
            while True:
                # also set a key with TTL to track live nodes (optional)
                key = f"node:{self.node_id}:ttl"
                try:
                    # use underlying connection to set TTL key
                    await self.redis._conn.set(key, "1", ex=self._hb_interval * 3)
                except Exception:
                    logger.exception("Failed to set heartbeat key")

                await self.redis.publish("cluster:events", f"HEARTBEAT:{self.node_id}")
                logger.debug("Heartbeat published for %s", self.node_id)
                await asyncio.sleep(self._hb_interval)
        except asyncio.CancelledError:
            logger.info("Heartbeat loop cancelled")

    async def _on_cluster_event(self, channel: str, message: str):
        # pesan sederhana; nanti kita definisikan format JSON
        logger.info("Node %s received event on %s: %s", self.node_id, channel, message)
        logger.debug(f"[{self.node_id}] Event received: {message}")

    def apply_command(self, command: str):
        """
        Apply a replicated command locally (simpan di log).
        """
        if not hasattr(self, "log"):
            self.log = []
        self.log.append(command)
        logger.info("[%s] Applied command: %s", self.node_id, command)


# ----- entrypoint ketika modul dieksekusi langsung -----
async def _run_from_env():
    node_id = os.getenv("NODE_ID", "node-unknown")
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    hb_interval = int(os.getenv("HEARTBEAT_INTERVAL", "5"))

    config = {
        "redis_url": redis_url,
        "heartbeat_interval": hb_interval
    }

    node = BaseNode(node_id=node_id, config=config)
    await node.start()

if __name__ == "__main__":
    try:
        asyncio.run(_run_from_env())
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting")
