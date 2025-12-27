# benchmarks/load_test_scenarios.py
import random
from locust import HttpUser, task, between, TaskSet

# --- Task Set untuk Menguji Lock Manager (Bagian A) ---
class LockManagerTasks(TaskSet):
    
    @task
    def acquire_and_release_lock(self):
        # Pilih resource dan owner secara acak
        resource_id = f"res_{random.randint(1, 10)}"
        owner_id = f"user_{self.user.user_id}"
        
        # 1. Acquire Lock (Exclusive)
        self.client.post("/lock", 
            json={
                "resource": resource_id,
                "owner": owner_id,
                "mode": "X"
            },
            name="/lock (Acquire)")
        
        # Simulasikan "pekerjaan"
        # time.sleep(random.uniform(0.1, 0.5)) 
        # (Lebih baik tidak sleep di locust, gunakan wait_time)
        
        # 2. Release Lock
        self.client.post("/unlock",
            json={
                "resource": resource_id,
                "owner": owner_id
            },
            name="/unlock (Release)")

# --- Task Set untuk Menguji Queue (Bagian B) ---
class QueueTasks(TaskSet):
    
    @task(3) # 3x lebih sering produce daripada consume
    def produce_task(self):
        queue_name = f"job_queue_{random.randint(1, 3)}"
        payload = {
            "user_id": random.randint(1, 1000),
            "task": "process_data"
        }
        
        self.client.post("/queue/produce",
            json={
                "queue_name": queue_name,
                "message": payload
            },
            name="/queue/produce")

    @task(1) # 1x consume
    def consume_task(self):
        queue_name = f"job_queue_{random.randint(1, 3)}"
        # Note: Consume hanya mengambil dari shard lokal node yang di-hit
        # Dalam load test, ini akan men-distribusi request consume
        with self.client.get(f"/queue/consume/{queue_name}", name="/queue/consume", catch_response=True) as response:
            if response.status_code == 404:
                # 404 is expected when queue is empty - not a failure
                response.success()

# --- Task Set untuk Menguji Cache (Bagian C) ---
class CacheTasks(TaskSet):
    
    @task(10) # 10x lebih sering GET
    def get_cache(self):
        key = f"cache_key_{random.randint(1, 20)}"
        # GET with catch_response to handle 404 gracefully
        with self.client.get(f"/cache/{key}", name="/cache (GET)", catch_response=True) as response:
            if response.status_code == 404:
                # 404 is expected for cache miss - not a failure
                response.success()

    @task(1) # 1x SET
    def set_cache(self):
        key = f"cache_key_{random.randint(1, 20)}"
        value = f"random_value_{random.randint(1, 10000)}"
        
        # Use new endpoint POST /cache with key and value in body
        self.client.post("/cache",
            json={"key": key, "value": value},
            name="/cache (SET)")

# --- User Utama yang Menjalankan Semua Task ---
class DistributedSystemUser(HttpUser):
    # Tunggu antara 0.5 sampai 1 detik antar task
    wait_time = between(0.5, 1.0)
    
    # Berikan bobot pada setiap skenario
    tasks = {
        LockManagerTasks: 2, # 20%
        QueueTasks: 3,       # 30%
        CacheTasks: 5        # 50% (Cache hit adalah skenario paling umum)
    }
    
    def on_start(self):
        """Dipanggil saat user virtual dimulai"""
        # Beri ID unik untuk user ini (berguna untuk lock owner)
        self.user_id = random.randint(1000, 9999)