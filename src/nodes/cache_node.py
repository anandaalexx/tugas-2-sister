# src/nodes/cache_node.py
import asyncio
import logging
from typing import Dict, Optional, Any
from enum import Enum

from src.cache.lru_cache import LRUCache
from src.utils.redis_client import RedisClient
from src.utils.message_client import MessageClient

logger = logging.getLogger(__name__)

class CacheState(Enum):
    """MESI Protocol States"""
    MODIFIED = "M"   # Cache memiliki data yang berbeda dengan memory (dirty)
    EXCLUSIVE = "E"  # Cache memiliki data sama dengan memory, tidak ada copy di cache lain
    SHARED = "S"     # Cache memiliki data sama dengan memory, mungkin ada copy di cache lain
    INVALID = "I"    # Data tidak valid, harus fetch dari memory atau cache lain

class DistributedCacheNode:
    """
    Implementasi Distributed Cache dengan MESI Coherence Protocol.
    
    Fitur:
    - MESI protocol untuk cache coherence
    - LRU cache replacement policy
    - Automatic invalidation dan update propagation
    - Performance metrics collection
    """
    
    def __init__(self, node_id: str, peers: Dict[str, str], redis: RedisClient, client: MessageClient, capacity: int = 100):
        self.node_id = node_id
        self.peers = peers
        self.redis = redis
        self.client = client
        
        # LRU Cache untuk menyimpan data
        self.cache = LRUCache(capacity)
        
        # State tracker untuk setiap key di cache (MESI Protocol)
        self.cache_states: Dict[str, CacheState] = {}
        
        # Performance metrics
        self.metrics = {
            "total_reads": 0,
            "total_writes": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "invalidations": 0,
            "updates": 0
        }

    async def start(self):
        """Start cache node dan subscribe ke channel invalidation"""
        logger.info(f"[{self.node_id}] Cache node starting...")
        await self.redis.subscribe("cache:invalidate", self._on_invalidation)
        await self.redis.subscribe("cache:update", self._on_update)
        logger.info(f"[{self.node_id}] Cache node started with capacity {self.cache.capacity}")

    async def get(self, key: str) -> Optional[str]:
        """
        Get value dari cache dengan MESI protocol.
        
        Returns:
            Value jika ada di cache, None jika miss
        """
        self.metrics["total_reads"] += 1
        
        # Cek state di cache
        state = self.cache_states.get(key, CacheState.INVALID)
        
        if state == CacheState.INVALID:
            # Cache miss - fetch dari "memory" (Redis)
            self.metrics["cache_misses"] += 1
            value = await self._fetch_from_memory(key)
            
            if value:
                # Beri tahu cache lain bahwa kita akan cache data ini (SHARED)
                await self._broadcast_read_intent(key)
                
                # Simpan di cache dengan state SHARED
                self.cache.put(key, value)
                self.cache_states[key] = CacheState.SHARED
                logger.debug(f"[{self.node_id}] Cache MISS for '{key}'. Fetched from memory. State: SHARED")
            
            return value
        else:
            # Cache hit - data ada di cache dan valid (M/E/S)
            self.metrics["cache_hits"] += 1
            value = self.cache.get(key)
            logger.debug(f"[{self.node_id}] Cache HIT for '{key}'. State: {state.value}")
            return value

    async def put(self, key: str, value: str):
        """
        Put value ke cache dengan MESI protocol.
        Akan menginvalidasi cache di node lain.
        """
        self.metrics["total_writes"] += 1
        
        # Kirim invalidation ke semua cache lain
        await self._broadcast_invalidation(key)
        
        # Tulis ke local cache dengan state MODIFIED
        self.cache.put(key, value)
        self.cache_states[key] = CacheState.MODIFIED
        
        # Tulis ke memory (Redis) untuk persistence
        await self._write_to_memory(key, value)
        
        logger.info(f"[{self.node_id}] Cache PUT '{key}'. State: MODIFIED. Sent invalidation to peers.")

    async def _fetch_from_memory(self, key: str) -> Optional[str]:
        """Fetch data dari 'memory' (Redis sebagai backing store)"""
        try:
            value = await self.redis._conn.get(f"data:{key}")
            return value.decode() if value else None
        except Exception as e:
            logger.error(f"[{self.node_id}] Error fetching from memory: {e}")
            return None

    async def _write_to_memory(self, key: str, value: str):
        """Write data ke 'memory' (Redis)"""
        try:
            await self.redis._conn.set(f"data:{key}", value)
        except Exception as e:
            logger.error(f"[{self.node_id}] Error writing to memory: {e}")

    async def _broadcast_invalidation(self, key: str):
        """
        Broadcast invalidation message ke semua cache nodes.
        MESI: Saat kita akan write, semua cache lain harus invalidate.
        """
        message = f"{self.node_id}:{key}"
        await self.redis.publish("cache:invalidate", message)
        logger.debug(f"[{self.node_id}] Broadcasted invalidation for '{key}'")

    async def _broadcast_read_intent(self, key: str):
        """
        Notify other caches bahwa kita read data ini.
        MESI: Cache lain yang punya data di state E akan transisi ke S.
        """
        message = f"{self.node_id}:{key}"
        await self.redis.publish("cache:update", message)
        logger.debug(f"[{self.node_id}] Notified read intent for '{key}'")

    async def _on_invalidation(self, channel: str, message: str):
        """
        Handle invalidation message dari cache lain.
        Format: "sender_node_id:key"
        """
        try:
            sender_node_id, key = message.split(":", 1)
            
            # Jangan invalidate jika message dari diri sendiri
            if sender_node_id == self.node_id:
                return
            
            # Invalidate key di cache lokal
            if key in self.cache_states:
                self.cache.delete(key)
                self.cache_states[key] = CacheState.INVALID
                self.metrics["invalidations"] += 1
                logger.info(f"[{self.node_id}] INVALIDATED '{key}' due to write from {sender_node_id}")
        
        except Exception as e:
            logger.error(f"[{self.node_id}] Error handling invalidation: {e}")

    async def _on_update(self, channel: str, message: str):
        """
        Handle update/read intent dari cache lain.
        MESI: Jika kita punya data di state E, transisi ke S.
        """
        try:
            sender_node_id, key = message.split(":", 1)
            
            if sender_node_id == self.node_id:
                return
            
            # Jika kita punya data di state EXCLUSIVE, transisi ke SHARED
            if self.cache_states.get(key) == CacheState.EXCLUSIVE:
                self.cache_states[key] = CacheState.SHARED
                self.metrics["updates"] += 1
                logger.debug(f"[{self.node_id}] Transitioned '{key}' from EXCLUSIVE to SHARED")
        
        except Exception as e:
            logger.error(f"[{self.node_id}] Error handling update: {e}")

    def get_metrics(self) -> Dict[str, Any]:
        """Return performance metrics"""
        cache_metrics = self.cache.get_metrics()
        
        return {
            "node_id": self.node_id,
            "cache_size": len(self.cache),
            "cache_capacity": self.cache.capacity,
            "total_reads": self.metrics["total_reads"],
            "total_writes": self.metrics["total_writes"],
            "cache_hits": self.metrics["cache_hits"],
            "cache_misses": self.metrics["cache_misses"],
            "hit_rate": cache_metrics.get("hit_rate", 0.0),
            "invalidations": self.metrics["invalidations"],
            "updates": self.metrics["updates"],
            "states": {k: v.value for k, v in self.cache_states.items()}
        }

    async def stop(self):
        """Stop cache node"""
        logger.info(f"[{self.node_id}] Cache node stopping...")
        # Cleanup jika diperlukan
