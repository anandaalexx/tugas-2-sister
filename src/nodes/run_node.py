import asyncio
import json
import logging
import os
from aiohttp import web
import aiohttp_cors  # <-- 1. IMPOR BARU

from src.utils.config import Config
from src.consensus.raft import RaftNode
from src.utils.message_client import MessageClient
from src.utils.redis_client import RedisClient
from src.nodes.queue_node import DistributedQueueNode
from src.utils.hashing import ConsistentHashRing
from src.cache.lru_cache import LRUCache

logging.basicConfig(level=Config.LOG_LEVEL)
# --- PERBAIKAN: Ganti nama logger __main__ agar tidak bentrok ---
logger = logging.getLogger("run_node")
# --- SELESAI PERBAIKAN ---

# --- Konfigurasi Peers ---
peers = {
    "node-1": "http://node1:8001",
    "node-2": "http://node2:8002",
    "node-3": "http://node3:8003"
}
peers_without_self = {k: v for k, v in peers.items() if k != Config.NODE_ID}
all_node_ids = list(peers.keys())

# --- BONUS B: Peta URL Peer ke Region ---
PEER_URL_TO_REGION = {
    "http://node1:8001": "US",
    "http://node2:8002": "US",
    "http://node3:8003": "ASIA",
}
# --- SELESAI BONUS B ---

# --- Konfigurasi Cache ---
CACHE_INVALIDATION_CHANNEL = "cache:invalidate"
CACHE_CAPACITY = 100


# --- HTTP Handlers (Bagian A: Raft Lock Manager) ---
# ... (Semua handler Anda, tidak ada perubahan di sini) ...
async def request_vote(request):
    """Internal RPC: Raft RequestVote"""
    data = await request.json()
    raft = request.app['raft']
    resp = await raft.handle_request_vote(data)
    return web.json_response(resp)

async def append_entries(request):
    """Internal RPC: Raft AppendEntries"""
    data = await request.json()
    raft = request.app['raft']
    resp = await raft.handle_append_entries(data)
    return web.json_response(resp)

async def handle_lock(request):
    """Client API: Request LOCK"""
    try:
        data = await request.json()
        
        resource = data['resource']
        owner = data['owner']
        mode = data.get('mode', 'X') 
        command = f"LOCK:{resource}:{owner}:{mode}"
        raft = request.app['raft']
        result = await raft.propose(command)
        
        # Audit Log
        if result.get("success") and result.get("data", {}).get("status") == "granted":
             logger.info(f"[AUDIT] SUCCESS Lock '{resource}' acquired by '{owner}'.")
            
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

async def handle_unlock(request):
    """Client API: Request UNLOCK"""
    try:
        data = await request.json()
        resource = data['resource']
        owner = data['owner']
        command = f"UNLOCK:{resource}:{owner}"
        raft = request.app['raft']
        result = await raft.propose(command)
        
        # --- BONUS D: Audit Log ---
        if result.get("success") and result.get("data", {}).get("status") == "unlocked":
             logger.info(f"[AUDIT] SUCCESS Lock '{resource}' released by '{owner}'.")
        # --- SELESAI BONUS D ---

        return web.json_response(result)
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

# --- HTTP Handlers (Bagian B: Distributed Queue) ---
async def handle_produce(request):
    """Client API: Produce pesan ke antrian"""
    try:
        data = await request.json()
        queue_name = data['queue_name']
        message = data['message']
        queue_node = request.app['queue_node']
        result = await queue_node.produce(queue_name, message)
        
        # --- BONUS D: Audit Log ---
        if result.get("success"):
            logger.info(f"[AUDIT] SUCCESS Message produced to queue '{queue_name}'.")
        # --- SELESAI BONUS D ---
            
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

async def handle_consume(request):
    """Client API: Consume pesan (dari shard lokal)"""
    try:
        queue_name = request.match_info['queue_name']
        queue_node = request.app['queue_node']
        message = await queue_node.consume(queue_name)
        if message:
            return web.json_response({"success": True, "message": message})
        else:
            return web.json_response({"success": False, "message": "Queue empty"}, status=404)
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

async def handle_produce_internal(request):
    """Internal RPC: Menerima produce request yang di-forward"""
    try:
        data = await request.json()
        queue_name = data['queue_name']
        message = data['message']
        queue_node = request.app['queue_node']
        nodes = queue_node._get_responsible_nodes(queue_name)
        result = await queue_node._handle_produce_and_replicate(queue_name, message, nodes)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

async def handle_replicate_queue(request):
    """Internal RPC: Menerima data replikasi dari Primary"""
    try:
        data = await request.json()
        queue_node = request.app['queue_node']
        result = await queue_node.handle_replicate_queue(data)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


# --- HTTP Handlers (Bagian C: Distributed Cache) ---

async def handle_cache_get(request):
    """Client API: GET data dari cache (Cache-Aside pattern)"""
    key = request.match_info['key']
    lru_cache = request.app['lru_cache']
    redis = request.app['redis']
    
    value = lru_cache.get(key)
    if value:
        return web.json_response({
            "key": key, 
            "value": value, 
            "source": "cache",
            "node_id": Config.NODE_ID
        })
    
    value_from_source = await redis.get(key)
    
    if value_from_source:
        lru_cache.put(key, value_from_source)
        return web.json_response({
            "key": key, 
            "value": value_from_source, 
            "source": "redis",
            "node_id": Config.NODE_ID
        })
    else:
        return web.json_response({"success": False, "message": "Key not found"}, status=404)

async def handle_cache_set(request):
    """Client API: SET data (Write-Through + Invalidation pattern)"""
    try:
        key = request.match_info['key']
        data = await request.json()
        value = data['value']
        
        lru_cache = request.app['lru_cache']
        redis = request.app['redis']
        
        await redis.set(key, value)
        lru_cache.put(key, value)
        await redis.publish(CACHE_INVALIDATION_CHANNEL, key)
        
        # --- BONUS D: Audit Log ---
        logger.info(f"[AUDIT] SUCCESS Cache SET for key '{key}'.")
        # --- SELESAI BONUS D ---
        
        return web.json_response({"success": True, "key": key, "value": value})
    except Exception as e:
        # --- PERBAIKAN TYPO 5G00 ---
        return web.json_response({"success": False, "message": str(e)}, status=500)
        # --- SELESAI PERBAIKAN ---

async def handle_cache_delete(request):
    """Client API: DELETE data (Write-Through + Invalidation pattern)"""
    try:
        key = request.match_info['key']
        lru_cache = request.app['lru_cache']
        redis = request.app['redis']

        await redis.delete(key)
        lru_cache.delete(key)
        await redis.publish(CACHE_INVALIDATION_CHANNEL, key)
        
        # --- BONUS D: Audit Log ---
        logger.info(f"[AUDIT] SUCCESS Cache DELETE for key '{key}'.")
        # --- SELESAI BONUS D ---
        
        return web.json_response({"success": True, "key": key})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

async def handle_cache_put(request):
    """Client API: PUT data to cache (accepts JSON body with key and value)"""
    try:
        data = await request.json()
        key = data['key']
        value = data['value']
        
        lru_cache = request.app['lru_cache']
        redis = request.app['redis']
        
        # Check if key already exists (UPDATE vs INSERT)
        existing_value = await redis.get(key)
        
        # Write-through: save to Redis first
        await redis.set(key, value)
        # Then cache locally
        lru_cache.put(key, value)
        
        # Only broadcast invalidation if this is an UPDATE (key existed before)
        invalidated_nodes = 0
        if existing_value:
            # Broadcast with node_id so sender can skip self-invalidation
            message = f"{key}|{Config.NODE_ID}"
            await redis.publish(CACHE_INVALIDATION_CHANNEL, message)
            invalidated_nodes = 2  # Other nodes get invalidated
            logger.info(f"[AUDIT] SUCCESS Cache UPDATE for key '{key}', invalidated {invalidated_nodes} nodes.")
        else:
            logger.info(f"[AUDIT] SUCCESS Cache PUT for key '{key}' (new entry).")
        
        return web.json_response({
            "success": True, 
            "key": key, 
            "value": value, 
            "cached": True,
            "invalidated_nodes": invalidated_nodes
        })
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)

# --- HTTP Handlers (Metrics) ---

async def handle_metrics(request):
    """Client API: GET performance metrics"""
    lru_cache = request.app['lru_cache']
    
    cache_metrics = lru_cache.get_metrics()
    
    return web.json_response({
        "node_id": Config.NODE_ID,
        "cache_metrics": cache_metrics
    })

async def handle_health(request):
    """Health check endpoint"""
    raft = request.app['raft']
    return web.json_response({
        "status": "healthy",
        "node_id": Config.NODE_ID,
        "role": raft.state,
        "term": raft.current_term
    })

async def handle_locks(request):
    """Get all locks status"""
    try:
        redis = request.app['redis']
        raft = request.app['raft']
        
        # Get all lock keys from Redis
        locks_data = {}
        lock_keys = await redis.keys("lock:*")
        
        for key in lock_keys:
            try:
                resource = key.replace("lock:", "")
                lock_info = await redis.get(key)
                if lock_info:
                    locks_data[resource] = json.loads(lock_info)
            except Exception as e:
                logger.error(f"Error parsing lock {key}: {e}")
                continue
        
        return web.json_response({
            "node_id": Config.NODE_ID,
            "role": raft.state,
            "locks": locks_data
        })
    except Exception as e:
        logger.error(f"Error in handle_locks: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_cache_metrics(request):
    """Get cache metrics endpoint"""
    lru_cache = request.app['lru_cache']
    cache_metrics = lru_cache.get_metrics()
    
    return web.json_response({
        "node_id": Config.NODE_ID,
        "metrics": cache_metrics
    })

async def handle_queue_metrics(request):
    """Get queue metrics endpoint"""
    try:
        redis = request.app['redis']
        
        # Get all queue keys from Redis
        queue_keys = await redis.keys("queue:*")
        
        queue_stats = {}
        for key in queue_keys:
            queue_name = key.replace("queue:", "")
            try:
                # Use raw connection for llen
                depth = await redis._conn.llen(key) if redis._conn else 0
                queue_stats[queue_name] = {
                    "depth": depth
                }
            except Exception as e:
                logger.error(f"Error getting queue depth for {key}: {e}")
                queue_stats[queue_name] = {"depth": 0}
        
        return web.json_response({
            "node_id": Config.NODE_ID,
            "total_queues": len(queue_stats),
            "queues": queue_stats
        })
    except Exception as e:
        logger.error(f"Error in handle_queue_metrics: {e}", exc_info=True)
        return web.json_response({"error": str(e)}, status=500)

# --- Fungsi Start/Stop (Gabungan) ---
async def start_background_tasks(app):
    """Mulai semua service: Redis, HTTP Client, Raft, Queue, dan Cache"""
    
    # 1. Buat Klien bersama (Shared Clients)
    logger.info("Connecting to Redis...")
    redis = RedisClient(url=Config.REDIS_URL)
    await redis.connect()
    app['redis'] = redis
    
    logger.info(f"Creating shared HTTP client for region: {Config.REGION}") 
    # --- PERUBAHAN BONUS B: Inisialisasi MessageClient dengan info region ---
    app['http_client'] = MessageClient(
        self_region=Config.REGION,
        peer_regions=PEER_URL_TO_REGION
    )
    # --- SELESAI PERUBAHAN ---

    # 2. Mulai Bagian A: RaftNode (Lock Manager)
    logger.info("Starting background Raft (Lock Manager) task...")
    
    # --- PERBAIKAN: Berikan http_client bersama ke RaftNode ---
    # Kita gunakan klien yang sama untuk Raft dan Queue
    raft = RaftNode(
        Config.NODE_ID, 
        peers_without_self, 
        redis, 
        app['http_client'] # Menggunakan klien yang sama
    )
    # --- SELESAI PERBAIKAN ---
    
    raft.config = {"deadlock_check_interval": 5} 
    app['raft'] = raft
    app['raft_task'] = asyncio.create_task(raft.run())
    
    # 3. Mulai Bagian B: DistributedQueueNode
    logger.info("Starting Distributed Queue Node service...")
    queue_node = DistributedQueueNode(
        node_id=Config.NODE_ID,
        all_nodes=all_node_ids,
        peers=peers,
        redis=app['redis'],
        client=app['http_client'] # Menggunakan klien yang sama
    )
    await queue_node.start()
    app['queue_node'] = queue_node

    # 4. Mulai Bagian C: LRUCache dan Invalidation Listener
    logger.info(f"Initializing LRU Cache with capacity {CACHE_CAPACITY}...")
    lru_cache = LRUCache(capacity=CACHE_CAPACITY)
    app['lru_cache'] = lru_cache
    
    # Buat callback untuk listener Pub/Sub
    async def on_invalidation_msg(channel: str, message: str):
        if message:
            # Message format: "key|sender_node_id"
            parts = message.split("|")
            key = parts[0]
            sender_node = parts[1] if len(parts) > 1 else None
            
            # Skip self-invalidation (don't delete cache if we're the one who updated)
            if sender_node == Config.NODE_ID:
                logger.info(f"[Cache] Skipping self-invalidation for key: {key}")
                return
                
            logger.info(f"[Cache] Received invalidation for: {key} from {sender_node}. Deleting from local cache.")
            lru_cache.delete(key)
            
    # Subscribe ke channel invalidasi
    logger.info(f"Subscribing to cache invalidation channel: {CACHE_INVALIDATION_CHANNEL}")
    await redis.subscribe(CACHE_INVALIDATION_CHANNEL, on_invalidation_msg)


async def cleanup_background_tasks(app):
    """Hentikan semua task dan tutup semua koneksi"""
    logger.info("Cleaning up Raft task...")
    app['raft_task'].cancel()
    try:
        await app['raft_task']
    except asyncio.CancelledError:
        pass
    
    logger.info("Closing clients (Shared HTTP, Shared Redis)...")
    
    # --- PERBAIKAN: Hapus duplikat close() ---
    # await app['raft'].client.close() # Dihapus, karena kita pakai http_client bersama
    await app['http_client'].close() # Cukup tutup satu kali
    # --- SELESAI PERBAIKAN ---
    
    # Unsubscribe sebelum menutup koneksi Redis
    logger.info(f"Unsubscribing from {CACHE_INVALIDATION_CHANNEL}...")
    try:
        await app['redis'].unsubscribe(CACHE_INVALIDATION_CHANNEL)
    except Exception as e:
        logger.warning(f"Error unsubscribing from Redis: {e}")
        
    await app['redis'].close()
    logger.info("Cleanup complete.")


# --- Setup Aplikasi (Diperbarui untuk CORS) ---
app = web.Application()

# --- 2. PERBAIKAN CORS ---
logger.info("Setting up CORS policies for Swagger UI...")
cors = aiohttp_cors.setup(app, defaults={
    # Izinkan localhost:8080 (tempat Swagger UI berjalan)
    "http://localhost:8080": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"),
})
# --- SELESAI PERBAIKAN ---


# --- 3. PERBAIKAN CORS: Terapkan CORS ke SETIAP route ---
# Endpoints Bagian A (Raft Lock Manager)
cors.add(app.router.add_post("/request_vote", request_vote))
cors.add(app.router.add_post("/append_entries", append_entries))
cors.add(app.router.add_post("/lock", handle_lock))
cors.add(app.router.add_post("/unlock", handle_unlock))

# Endpoints Bagian B (Distributed Queue)
cors.add(app.router.add_post("/queue/produce", handle_produce))
cors.add(app.router.add_get("/queue/consume/{queue_name}", handle_consume))
cors.add(app.router.add_post("/produce_internal", handle_produce_internal))
cors.add(app.router.add_post("/replicate_queue", handle_replicate_queue))

# Endpoints Bagian C (Distributed Cache)
cors.add(app.router.add_post("/cache", handle_cache_put))  # New: POST /cache with JSON body
cors.add(app.router.add_get("/cache/{key}", handle_cache_get))
cors.add(app.router.add_post("/cache/{key}", handle_cache_set))
cors.add(app.router.add_delete("/cache/{key}", handle_cache_delete))

# Endpoints Metrics & Health
cors.add(app.router.add_get("/metrics", handle_metrics))
cors.add(app.router.add_get("/health", handle_health))
cors.add(app.router.add_get("/locks", handle_locks))
cors.add(app.router.add_get("/cache/metrics", handle_cache_metrics))
cors.add(app.router.add_get("/queue/metrics", handle_queue_metrics))
# --- SELESAI PERBAIKAN ---


# Startup & Cleanup Hooks
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    logger.info(f"Starting server for {Config.NODE_ID} on port {Config.PORT}")
    web.run_app(app, host="0.0.0.0", port=Config.PORT)