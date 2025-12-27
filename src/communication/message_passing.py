import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

class MessageClient:
    """Mengirim pesan antar node."""
    def __init__(self):
        self.session = aiohttp.ClientSession()

    async def send(self, target_url: str, endpoint: str, data: dict):
        """Kirim POST ke node lain."""
        try:
            async with self.session.post(f"{target_url}/{endpoint}", json=data) as resp:
                logger.info(f"Sent to {target_url}/{endpoint}: {data}")
                return await resp.json()
        except Exception as e:
            logger.error(f"Error sending to {target_url}: {e}")
            return None

    async def close(self):
        await self.session.close()
