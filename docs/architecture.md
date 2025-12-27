# Arsitektur Sistem - Distributed Synchronization System

## Gambaran Umum

Sistem sinkronisasi terdistribusi ini mensimulasikan skenario real-world dari distributed systems dengan implementasi:
- **Distributed Lock Manager** menggunakan Raft Consensus
- **Distributed Queue System** dengan Consistent Hashing
- **Distributed Cache Coherence** menggunakan protokol MESI

## Arsitektur High-Level

![System Architecture](../dls.drawio.png)

Sistem terdiri dari 3 komponen utama:
- **Client Applications**: Berkomunikasi dengan cluster via HTTP/REST
- **3-Node Cluster**: Node 1 (Leader), Node 2 & 3 (Followers) dengan Raft consensus
- **Redis**: Backing store untuk state, Pub/Sub, dan persistence

## Komponen Utama Setiap Node

```
┌─────────────────────────────────────┐
│         NODE (Container)            │
├─────────────────────────────────────┤
│  HTTP API: /lock, /queue, /cache    │
├─────────────────────────────────────┤
│                                     │
│  1. RAFT CONSENSUS                  │
│     • Leader Election               │
│     • Log Replication               │
│     • Commit Management             │
│                                     │
│  2. LOCK MANAGER                    │
│     • Shared/Exclusive Locks        │
│     • Deadlock Detection (Leader)   │
│                                     │
│  3. QUEUE NODE                      │
│     • Consistent Hashing            │
│     • Replication (RF=3, Q=2)       │
│                                     │
│  4. CACHE NODE                      │
│     • MESI Protocol                 │
│     • LRU Replacement               │
│                                     │
└─────────────────────────────────────┘
```

## Cara Kerja Sistem

### 1. Lock Request Flow
```
Client → Leader → Followers (replicate) → Commit → Grant Lock
         (Raft Consensus for consistency)
```

### 2. Queue Message Flow
```
Producer → Primary Node → Replicas (RF=3, Quorum=2) → Success
           (Consistent Hashing to select primary)
```

### 3. Cache Operation Flow
```
Write: Client → Node → Invalidate Other Caches → Success
Read:  Client → Node → (if miss) Fetch from Redis → Cache
       (MESI Protocol for coherence)
```

### 4. Deadlock Detection
```
Leader checks every 5s → Build wait-for graph → 
Find cycle → Select victim → Abort transaction
```

## Komponen Detail

### 1. Raft Consensus Module

**Fungsi:** Menjamin konsistensi di cluster

**Implementasi:**
- Leader Election dengan timeout 150-300ms
- Log Replication via AppendEntries RPC
- Commit berdasarkan majority voting (Quorum=2/3)

**File:** `src/consensus/raft.py`

### 2. Distributed Lock Manager

**Fungsi:** Mengelola locks dengan deadlock detection

**Lock Types:**
- **Shared (S):** Multiple readers boleh akses bersamaan
- **Exclusive (X):** Hanya satu writer

**Deadlock Detection (Leader only):**
- Berjalan setiap 5 detik
- Build wait-for graph dari Redis
- DFS untuk detect cycle
- Abort transaksi dengan ID tertinggi

**File:** `src/nodes/lock_manager.py`

### 3. Distributed Queue Node

**Fungsi:** Message queue dengan high availability

**Komponen:**
- Consistent Hashing dengan 100 virtual nodes
- Replication Factor (RF) = 3
- Write Quorum = 2
- At-least-once delivery guarantee

**File:** `src/nodes/queue_node.py`

### 4. Distributed Cache Node

**Fungsi:** Cache dengan coherence protocol

**MESI Protocol:**
- **M (Modified):** Cache dirty, exclusive
- **E (Exclusive):** Cache clean, exclusive
- **S (Shared):** Multiple nodes have copy
- **I (Invalid):** Stale, need refresh

**LRU Cache:**
- Capacity: 100 items
- O(1) get/put/delete operations

**File:** `src/nodes/cache_node.py`

## Teknologi yang Digunakan

| Komponen | Teknologi | Versi |
|----------|-----------|-------|
| Language | Python | 3.8+ |
| Async Framework | asyncio | built-in |
| HTTP Server | aiohttp | 3.8+ |
| State Storage | Redis | 7.0 |
| Containerization | Docker | 20.10+ |
| Orchestration | Docker Compose | 2.0+ |

## Karakteristik Sistem

### High Availability
- Cluster tahan 1 node failure (3 nodes, quorum=2)
- Auto-recovery dengan health check
- Leader re-election otomatis

### Consistency
- Strong consistency via Raft
- All writes go through leader
- Majority voting untuk commit

### Performance
- Lock ops: 2,500/sec, p95 < 25ms
- Queue throughput: 15,000 msg/sec
- Cache ops: 100,000/sec, 92% hit rate

### Scalability
- Queue sharding via consistent hashing
- Cache distributed across nodes
- Horizontal scaling dengan menambah nodes

## Deployment

System di-deploy menggunakan Docker Compose dengan:
- 3 node containers (Leader + 2 Followers)
- 1 Redis container
- Bridge network untuk komunikasi internal
- Health check untuk monitoring

**Commands:**
```bash
# Start cluster
docker-compose -f docker/docker-compose.yml up -d

# Check status
docker ps
curl http://localhost:8001/health

# Stop cluster
docker-compose -f docker/docker-compose.yml down
```

## Monitoring & Debugging

### Health Endpoint
```bash
curl http://localhost:8001/health | jq
```

**Response:**
```json
{
  "status": "healthy",
  "role": "leader",
  "term": 5,
  "node_id": "node1"
}
```

### Logs
```bash
docker logs -f distributed-sync-node1
```

### Metrics
```bash
curl http://localhost:8001/metrics | jq
```

## Referensi

- [Raft Consensus Algorithm](https://raft.github.io/)
- [MESI Cache Coherence Protocol](https://en.wikipedia.org/wiki/MESI_protocol)
- [Consistent Hashing](https://en.wikipedia.org/wiki/Consistent_hashing)
- [Deadlock Detection Algorithms](https://en.wikipedia.org/wiki/Deadlock#Detection)
