import aiohttp
import asyncio
import logging
from typing import Dict # --- BONUS B ---

logger = logging.getLogger(__name__)

# --- BONUS B: Peta latensi (dalam detik) ---
# Jika dari US -> ASIA, tambahkan 300ms delay
REGION_LATENCY_MAP = {
    ("US", "ASIA"): 0.3,
    ("ASIA", "US"): 0.3,
    # (US ke US atau ASIA ke ASIA = 0)
}
# --- SELESAI BONUS B ---

class MessageClient:
    """Mengirim pesan antar node."""
    
    # --- BONUS B: Modifikasi __init__ ---
    def __init__(self, self_region: str, peer_regions: Dict[str, str]):
        self.session = aiohttp.ClientSession()
        self.self_region = self_region
        # peer_regions akan terlihat seperti: 
        # {"http://node1:8001": "US", "http://node3:8003": "ASIA"}
        self.peer_regions = peer_regions
        logger.info(f"MessageClient started in region {self_region}.")
    # --- SELESAI BONUS B ---

    async def send(self, target_url: str, endpoint: str, data: dict):
        """Kirim POST ke node lain."""
        
        # --- BONUS B: Simulasi Latensi ---
        target_region = self.peer_regions.get(target_url)
        
        if target_region and self.self_region != target_region:
            latency = REGION_LATENCY_MAP.get((self.self_region, target_region), 0)
            if latency > 0:
                logger.warning(f"SIMULATING {latency*1000}ms latency: {self.self_region} -> {target_region} ({target_url})")
                await asyncio.sleep(latency)
        # --- SELESAI BONUS B ---
        
        full_url = f"{target_url}/{endpoint}"
        try:
            # Gunakan session yang sudah ada
            async with self.session.post(full_url, json=data) as resp:
                # logger.info(f"Sent to {full_url}: {data}") # Dikomentari agar tidak banjir log
                resp.raise_for_status() # Penting untuk check error HTTP
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"Error sending to {full_url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending to {full_url}: {e}")
            return None

    async def close(self):
        await self.session.close()