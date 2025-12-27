# src/utils/redis_client.py
import asyncio
import os
import logging
from typing import Callable, Awaitable

import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

class RedisClient:
    def __init__(self, url: str = REDIS_URL):
        self.url = url
        self._conn: redis.Redis | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._subs: dict[str, tuple[redis.client.PubSub, Callable]] = {}

    async def connect(self):
        if self._conn is None:
            self._conn = redis.from_url(self.url, decode_responses=True)
            try:
                await self._conn.ping()
            except Exception as e:
                logger.error("Redis ping failed: %s", e)
                raise
            logger.info("Connected to Redis at %s", self.url)

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def publish(self, channel: str, message: str):
        if not self._conn:
            raise RuntimeError("Redis not connected")
        return await self._conn.publish(channel, message)

    async def subscribe(self, channel: str, callback: Callable[[str, str], Awaitable[None]]):
        if not self._conn:
            raise RuntimeError("Redis not connected")

        pubsub = self._conn.pubsub()
        await pubsub.subscribe(channel)
        self._subs[channel] = (pubsub, callback)

        if self._pubsub_task is None:
            self._pubsub_task = asyncio.create_task(self._pubsub_listener())

    async def _pubsub_listener(self):
        try:
            while True:
                for channel, (pubsub, callback) in list(self._subs.items()):
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                    if msg:
                        data = msg.get("data")
                        if data is not None:
                            await callback(channel, data)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            logger.info("PubSub listener cancelled")
        except Exception:
            logger.exception("PubSub listener crashed")

    async def unsubscribe(self, channel: str):
        if channel in self._subs:
            pubsub, _ = self._subs.pop(channel)
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        if not self._subs and self._pubsub_task:
            self._pubsub_task.cancel()
            self._pubsub_task = None

    # --- Redis data commands ---
    async def set(self, key: str, value: str):
        if self._conn is None: await self.connect()
        return await self._conn.set(key, value)

    async def get(self, key: str):
        if self._conn is None: await self.connect()
        return await self._conn.get(key)

    async def delete(self, key: str):
        if self._conn is None: await self.connect()
        return await self._conn.delete(key)

    async def sadd(self, key: str, *members):
        if self._conn is None: await self.connect()
        return await self._conn.sadd(key, *members)

    async def smembers(self, key: str):
        if self._conn is None: await self.connect()
        return await self._conn.smembers(key)

    async def keys(self, pattern: str):
        if self._conn is None: await self.connect()
        return await self._conn.keys(pattern)

    async def hset(self, name: str, mapping: dict):
        if self._conn is None: await self.connect()
        return await self._conn.hset(name, mapping=mapping)
    
    async def hgetall(self, name: str):
        if self._conn is None: await self.connect()
        return await self._conn.hgetall(name)

    async def hdel(self, name: str, *keys):
        if self._conn is None: await self.connect()
        return await self._conn.hdel(name, *keys)

    async def exists(self, key: str):
        if self._conn is None: await self.connect()
        return await self._conn.exists(key)
