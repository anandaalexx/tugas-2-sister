import asyncio
import aioredis
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
COMMAND = "SET x=10"

async def main():
    # buat Redis client, connect otomatis
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Ambil leader yang sedang aktif dari Redis
    leader_id = await redis.get("current_leader")
    if not leader_id:
        logger.warning("Tidak ada leader saat ini! Pastikan cluster berjalan.")
        return

    logger.info(f"Leader saat ini: {leader_id}")

    # Publish command ke channel cluster:events
    msg = f"CMD:{COMMAND}"
    await redis.publish("cluster:events", msg)
    logger.info(f"Sent command to cluster: {COMMAND}")

    # Tunggu sebentar agar followers bisa apply
    await asyncio.sleep(2)

    # Cek nilai di Redis
    val = await redis.get("x")
    logger.info(f"Value of x after command: {val}")

    await redis.close()

if __name__ == "__main__":
    asyncio.run(main())
