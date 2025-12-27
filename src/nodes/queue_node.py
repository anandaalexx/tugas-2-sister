# src/nodes/queue_node.py
import asyncio
import json
import logging
from typing import Dict, List, Any, Optional

from src.utils.redis_client import RedisClient
from src.utils.hashing import ConsistentHashRing
from src.utils.message_client import MessageClient # BARU

logger = logging.getLogger(__name__)

class DistributedQueueNode:
    
    def __init__(self, node_id: str, all_nodes: List[str], peers: Dict[str, str], redis: RedisClient, client: MessageClient):
        self.node_id = node_id
        # Inisialisasi hash ring dengan SEMUA node yang bisa jadi broker
        self.hash_ring = ConsistentHashRing(all_nodes) 
        self.redis = redis # Gunakan RedisClient yang sudah ada
        self.client = client # Gunakan MessageClient untuk RPC
        self.peers = peers # Daftar URL peer, misal {"node-2": "http://node2:8002"}
        
        self.queue_prefix = "queue:"
        self.processing_prefix = "processing:"
        
        # Jumlah replika data (Total = 1 Primary + (REPLICATION_FACTOR-1) Replika)
        self.REPLICATION_FACTOR = 3 # Harus <= jumlah node
        # Jumlah ACK minimum yang ditunggu (Quorum)
        self.WRITE_QUORUM = 2 # (REPLICATION_FACTOR // 2) + 1

    async def start(self):
        # Redis sudah di-connect dari main.py
        logger.info(f"[{self.node_id}] Queue node started.")
        # Kita juga perlu recover_unacked saat startup
        # (Perlu logic untuk menemukan queue mana yang jadi tanggung jawab kita)

    def _get_responsible_nodes(self, queue_name: str) -> List[str]:
        """Helper untuk dapatkan daftar node (Primary + Replika)"""
        return self.hash_ring.get_nodes(queue_name, self.REPLICATION_FACTOR)

    async def produce(self, queue_name: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Public API untuk producer.
        Menangani forwarding jika request salah kirim.
        """
        nodes = self._get_responsible_nodes(queue_name)
        if not nodes:
            return {"success": False, "message": "No available nodes in hash ring."}
            
        primary_node = nodes[0]
        
        if primary_node == self.node_id:
            # === KITA ADALAH PRIMARY ===
            # Langsung panggil logic internal untuk replikasi
            return await self._handle_produce_and_replicate(queue_name, message, nodes)
        else:
            # === KITA BUKAN PRIMARY ===
            # Forward request ke node primary yang benar
            primary_url = self.peers.get(primary_node)
            if not primary_url:
                logger.error(f"[{self.node_id}] Cannot find URL for primary {primary_node}")
                return {"success": False, "message": f"Cannot contact primary node {primary_node}"}
            
            logger.info(f"[{self.node_id}] Forwarding produce request for '{queue_name}' to {primary_node}")
            payload = {"queue_name": queue_name, "message": message}
            response = await self.client.send(primary_url, "produce_internal", payload) # Endpoint baru
            
            if response:
                return response
            else:
                return {"success": False, "message": f"Failed to forward request to {primary_node}"}

    async def _handle_produce_and_replicate(self, queue_name: str, message: Dict[str, Any], nodes: List[str]) -> Dict[str, Any]:
        """
        (INTERNAL) Hanya dipanggil oleh Primary.
        Menulis ke local Redis dan mereplikasi ke peers.
        """
        message_json = json.dumps(message)
        
        # 1. Tulis ke Redis Lokal (Primary)
        try:
            key = f"{self.queue_prefix}{self.node_id}:{queue_name}"
            await self.redis._conn.rpush(key, message_json)
        except Exception as e:
            logger.error(f"[{self.node_id}] Failed to write to local Redis: {e}")
            return {"success": False, "message": "Failed to write to primary storage."}
            
        acks_needed = self.WRITE_QUORUM - 1 # (1 ACK sudah dari diri sendiri)
        if acks_needed <= 0:
            return {"success": True, "message": "Written to primary (no replicas needed)."}

        # 2. Replikasi ke Replicas
        replica_nodes = nodes[1:]
        tasks = []
        payload = {"queue_name": queue_name, "message_json": message_json, "sender_id": self.node_id}
        
        for replica_id in replica_nodes:
            replica_url = self.peers.get(replica_id)
            if replica_url:
                logger.debug(f"[{self.node_id}] Replicating '{queue_name}' to {replica_id}")
                tasks.append(
                    asyncio.create_task(self.client.send(replica_url, "replicate_queue", payload))
                )
        
        # 3. Tunggu Quorum
        acks_received = 0
        for task in asyncio.as_completed(tasks):
            try:
                response = await task
                if response and response.get("success"):
                    acks_received += 1
                    if acks_received >= acks_needed:
                        # Quorum tercapai!
                        logger.info(f"[{self.node_id}] Produce for '{queue_name}' reached quorum ({acks_received+1}/{self.REPLICATION_FACTOR})")
                        # (Kita bisa cancel task yg belum selesai, tapi tidak wajib)
                        return {"success": True, "message": "Write quorum achieved."}
            except Exception as e:
                logger.warning(f"[{self.node_id}] Replication failed for one node: {e}")

        # Jika loop selesai tapi quorum tidak tercapai
        logger.error(f"[{self.node_id}] Failed to achieve write quorum for '{queue_name}' (Got {acks_received+1}/{self.WRITE_QUORUM})")
        return {"success": False, "message": "Failed to achieve write quorum."}


    async def handle_replicate_queue(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        (INTERNAL RPC) Dipanggil oleh Primary untuk menyimpan replika.
        """
        try:
            queue_name = request_data['queue_name']
            message_json = request_data['message_json']
            
            # Simpan di Redis lokal kita (sebagai replika)
            key = f"{self.queue_prefix}{self.node_id}:{queue_name}"
            await self.redis._conn.rpush(key, message_json)
            
            logger.debug(f"[{self.node_id}] Successfully replicated message for '{queue_name}'")
            return {"success": True}
        except Exception as e:
            logger.error(f"[{self.node_id}] Failed to handle replication request: {e}")
            return {"success": False, "message": str(e)}

    # --- Consume & Ack (Tidak Berubah) ---
    # Consume HANYA dari shard lokal. Ini sudah benar.
    
    async def consume(self, queue_name: str) -> Optional[Dict[str, Any]]:
        """Consume dari shard lokal node ini."""
        key = f"{self.queue_prefix}{self.node_id}:{queue_name}"
        msg = await self.redis._conn.lpop(key)
        
        if msg:
            proc_key = f"{self.processing_prefix}{self.node_id}:{queue_name}"
            await self.redis._conn.rpush(proc_key, msg)
            data = json.loads(msg)
            logger.info(f"[{self.node_id}] Consumed message: {data}")
            return data
        return None

    async def ack(self, queue_name: str, message: Dict[str, Any]):
        """Ack (hapus) dari processing list lokal."""
        proc_key = f"{self.processing_prefix}{self.node_id}:{queue_name}"
        await self.redis._conn.lrem(proc_key, 0, json.dumps(message))
        logger.info(f"[{self.node_id}] Acked message: {message}")

    async def recover_unacked(self, queue_name: str):
        """(Recovery) Kembalikan pesan yg belum di-ACK ke queue lokal."""
        proc_key = f"{self.processing_prefix}{self.node_id}:{queue_name}"
        key = f"{self.queue_prefix}{self.node_id}:{queue_name}"
        
        # Pindahkan semua dari processing kembali ke queue (atomic)
        while True:
            msg = await self.redis._conn.rpoplpush(proc_key, key)
            if msg is None:
                break # Selesai
            logger.warning(f"[{self.node_id}] Recovered unacked message for '{queue_name}'")