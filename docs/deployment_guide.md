# Deployment Guide - Distributed Synchronization System

## Prasyarat

### Software Requirements
- **Docker:** Version 20.10 atau lebih baru
- **Docker Compose:** Version 2.0 atau lebih baru
- **Python:** 3.8+ (untuk development lokal)
- **Git:** Untuk clone repository

### Hardware Requirements (Minimum)
- **CPU:** 2 cores
- **RAM:** 4 GB
- **Disk:** 10 GB free space
- **Network:** Koneksi internet untuk pull images

### Hardware Requirements (Recommended)
- **CPU:** 4+ cores
- **RAM:** 8+ GB
- **Disk:** 20 GB free space
- **Network:** Low latency (<50ms antar nodes)

## Quick Start (Docker Compose)

### 1. Clone Repository

```bash
git clone https://github.com/your-username/distributed-sync-system.git
cd distributed-sync-system
```

### 2. Setup Environment

```bash
# Copy environment example
cp .env.example .env

# Edit jika perlu (optional)
nano .env
```

### 3. Build dan Run

```bash
# Build images
docker-compose -f docker/docker-compose.yml build

# Start all services
docker-compose -f docker/docker-compose.yml up -d

# Check logs
docker-compose -f docker/docker-compose.yml logs -f
```

### 4. Verify Deployment

```bash
# Check running containers
docker ps

# Should see:
# - redis
# - node1 (port 8001)
# - node2 (port 8002)
# - node3 (port 8003)

# Test node health
curl http://localhost:8001/health
curl http://localhost:8002/health
curl http://localhost:8003/health
```

## Development Setup (Local)

### 1. Setup Python Virtual Environment

```bash
# Create virtual environment
python3 -m venv venv

# Activate (Linux/Mac)
source venv/bin/activate

# Activate (Windows)
venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Start Redis

```bash
# Option 1: Docker
docker run -d -p 6379:6379 --name redis redis:7-alpine

# Option 2: Local Redis
redis-server
```

### 4. Run Nodes Manually

```bash
# Terminal 1 - Node 1
export NODE_ID=node-1
export PORT=8001
export REDIS_URL=redis://localhost:6379/0
python src/nodes/run_node.py

# Terminal 2 - Node 2
export NODE_ID=node-2
export PORT=8002
export REDIS_URL=redis://localhost:6379/0
python src/nodes/run_node.py

# Terminal 3 - Node 3
export NODE_ID=node-3
export PORT=8003
export REDIS_URL=redis://localhost:6379/0
python src/nodes/run_node.py
```

## Configuration

### Environment Variables

Setiap node dapat dikonfigurasi melalui environment variables:

```bash
# Node Identity
NODE_ID=node-1              # Unique identifier untuk node
PORT=8001                   # HTTP port untuk API
REGION=US                   # Geographic region (opsional)

# Redis Configuration
REDIS_URL=redis://redis:6379/0

# Cluster Peers (comma-separated)
PEERS=node-2:http://node2:8002,node-3:http://node3:8003

# Cache Settings
CACHE_CAPACITY=100          # Max number of items in cache

# Raft Configuration
HEARTBEAT_INTERVAL=50       # milliseconds
ELECTION_TIMEOUT_MIN=150    # milliseconds
ELECTION_TIMEOUT_MAX=300    # milliseconds

# Deadlock Detection
DEADLOCK_CHECK_INTERVAL=5   # seconds

# Queue Configuration
REPLICATION_FACTOR=3        # Number of replicas
WRITE_QUORUM=2              # Minimum ACKs for write

# Monitoring
ENABLE_METRICS=true
METRICS_PORT=9090
LOG_LEVEL=INFO              # DEBUG, INFO, WARNING, ERROR
```

### Docker Compose Configuration

Edit `docker/docker-compose.yml` untuk:

1. **Menambah Nodes:**

```yaml
  node4:
    build:
      context: ..
      dockerfile: docker/Dockerfile.node
    environment:
      - NODE_ID=node-4
      - PORT=8004
      - REDIS_URL=redis://redis:6379/0
    command: python src/nodes/run_node.py
    depends_on:
      - redis
    ports:
      - "8004:8004"
    volumes:
      - ../:/app
    networks:
      - dist_network
```

2. **Resource Limits:**

```yaml
  node1:
    # ... existing config ...
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M
```

## Scaling

### Horizontal Scaling

Untuk menambah nodes secara dinamis:

```bash
# Scale to 5 nodes
docker-compose -f docker/docker-compose.yml up -d --scale node1=5

# Atau dengan docker swarm
docker stack deploy -c docker/docker-compose.yml mystack
docker service scale mystack_node1=5
```

### Vertical Scaling

Update resource limits di docker-compose.yml:

```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 2G
```

## Monitoring

### Log Collection

```bash
# View all logs
docker-compose -f docker/docker-compose.yml logs -f

# View specific node
docker-compose -f docker/docker-compose.yml logs -f node1

# Save logs to file
docker-compose -f docker/docker-compose.yml logs > system.log
```

### Metrics Endpoint

Setiap node expose metrics di `/metrics`:

```bash
curl http://localhost:8001/metrics
```

Response:
```json
{
  "node_id": "node-1",
  "role": "leader",
  "cache_hit_rate": 85.5,
  "lock_requests": 1250,
  "queue_messages": 5000,
  "uptime_seconds": 3600
}
```

### Prometheus Setup (Optional)

1. **Create prometheus.yml:**

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'distributed-sync'
    static_configs:
      - targets: 
        - 'node1:9090'
        - 'node2:9090'
        - 'node3:9090'
```

2. **Add to docker-compose.yml:**

```yaml
  prometheus:
    image: prom/prometheus
    ports:
      - "9091:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    networks:
      - dist_network
```

## Testing

### Unit Tests

```bash
# Run all unit tests
pytest tests/unit/

# With coverage
pytest --cov=src tests/unit/
```

### Integration Tests

```bash
# Ensure cluster is running
docker-compose -f docker/docker-compose.yml up -d

# Run integration tests
pytest tests/integration/
```

### Performance Tests

```bash
# Install locust
pip install locust

# Run load test
locust -f benchmarks/load_test_scenarios.py --host=http://localhost:8001
```

Buka browser ke `http://localhost:8089` untuk Web UI.

## Troubleshooting

### Problem: Nodes tidak bisa communicate

**Symptom:** Logs menunjukkan connection refused atau timeout

**Solution:**
```bash
# Check network
docker network ls
docker network inspect distributed-sync-system_dist_network

# Ensure all containers di network yang sama
docker-compose -f docker/docker-compose.yml down
docker-compose -f docker/docker-compose.yml up -d
```

### Problem: Redis connection error

**Symptom:** `redis.exceptions.ConnectionError`

**Solution:**
```bash
# Check Redis running
docker ps | grep redis

# Check Redis logs
docker logs redis

# Restart Redis
docker-compose -f docker/docker-compose.yml restart redis
```

### Problem: Split brain (Multiple leaders)

**Symptom:** Logs menunjukkan lebih dari satu leader

**Solution:**
```bash
# Restart cluster untuk re-election
docker-compose -f docker/docker-compose.yml restart

# Monitor logs
docker-compose -f docker/docker-compose.yml logs -f | grep "LEADER"
```

### Problem: High memory usage

**Symptom:** Container OOM atau slow response

**Solution:**
```bash
# Check memory usage
docker stats

# Reduce cache capacity di .env
CACHE_CAPACITY=50

# Add memory limits
# Edit docker-compose.yml (see Resource Limits section)
```

### Problem: Deadlock detection tidak berjalan

**Symptom:** Lock requests hang indefinitely

**Solution:**
```bash
# Check leader status
curl http://localhost:8001/status

# Deadlock detection hanya jalan di leader
# Jika tidak ada leader, trigger election dengan restart
docker-compose -f docker/docker-compose.yml restart node1
```

## Backup & Recovery

### Backup Redis Data

```bash
# Backup
docker exec redis redis-cli SAVE
docker cp redis:/data/dump.rdb ./backup/dump-$(date +%Y%m%d).rdb

# Restore
docker cp ./backup/dump.rdb redis:/data/dump.rdb
docker-compose -f docker/docker-compose.yml restart redis
```

### Full System Backup

```bash
# Stop cluster
docker-compose -f docker/docker-compose.yml down

# Backup volumes
docker run --rm -v distributed-sync-system_redis-data:/data \
  -v $(pwd)/backup:/backup alpine \
  tar czf /backup/redis-data.tar.gz -C /data .

# Restore
docker run --rm -v distributed-sync-system_redis-data:/data \
  -v $(pwd)/backup:/backup alpine \
  tar xzf /backup/redis-data.tar.gz -C /data

# Start cluster
docker-compose -f docker/docker-compose.yml up -d
```

## Upgrading

### Rolling Update

```bash
# Update code
git pull origin main

# Rebuild image
docker-compose -f docker/docker-compose.yml build

# Update nodes one by one
docker-compose -f docker/docker-compose.yml up -d --no-deps node1
sleep 10
docker-compose -f docker/docker-compose.yml up -d --no-deps node2
sleep 10
docker-compose -f docker/docker-compose.yml up -d --no-deps node3
```

### Blue-Green Deployment

```bash
# Start new cluster (green)
docker-compose -f docker/docker-compose-green.yml up -d

# Test new cluster
# ... run tests ...

# Switch traffic (update load balancer)
# Stop old cluster (blue)
docker-compose -f docker/docker-compose.yml down
```

## Security

### Network Security

```bash
# Use TLS for inter-node communication
# Add to docker-compose.yml:
environment:
  - TLS_ENABLED=true
  - TLS_CERT=/certs/server.crt
  - TLS_KEY=/certs/server.key
volumes:
  - ./certs:/certs:ro
```

### Redis Authentication

```bash
# Edit docker-compose.yml
redis:
  command: redis-server --requirepass yourpassword

# Update nodes
environment:
  - REDIS_URL=redis://:yourpassword@redis:6379/0
```

### Firewall Rules

```bash
# Allow only cluster communication
iptables -A INPUT -p tcp --dport 8001:8003 -s 10.0.0.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 8001:8003 -j DROP
```

## Production Checklist

- [ ] Set proper resource limits
- [ ] Enable TLS/SSL
- [ ] Setup monitoring (Prometheus/Grafana)
- [ ] Configure log aggregation (ELK/Loki)
- [ ] Setup automated backups
- [ ] Configure alerting
- [ ] Load test dengan production-like load
- [ ] Document runbook untuk on-call
- [ ] Setup CI/CD pipeline
- [ ] Review security hardening

## Support

Untuk issues atau questions:
- **GitHub Issues:** https://github.com/your-username/distributed-sync-system/issues
- **Documentation:** https://github.com/your-username/distributed-sync-system/docs
- **Email:** your-email@example.com
