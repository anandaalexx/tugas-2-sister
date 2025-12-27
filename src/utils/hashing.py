# src/utils/hashing.py
import hashlib
import bisect
from typing import List, Optional, Set

class ConsistentHashRing:
    def __init__(self, nodes: Optional[List[str]] = None, replicas: int = 3):
        """
        replicas: Jumlah virtual node per physical node
        """
        self.virtual_replicas = replicas
        self.ring: Dict[int, str] = {}
        self.sorted_keys: List[int] = []
        self.nodes: Set[str] = set() # Lacak node fisik
        
        if nodes:
            for node in nodes:
                self.add_node(node)

    def _hash(self, key: str) -> int:
        return int(hashlib.md5(key.encode('utf-8')).hexdigest(), 16)

    def add_node(self, node: str):
        """Tambah node fisik ke ring"""
        if node in self.nodes:
            return # Sudah ada
            
        self.nodes.add(node)
        for i in range(self.virtual_replicas):
            key = self._hash(f"{node}:{i}")
            self.ring[key] = node
            bisect.insort(self.sorted_keys, key)

    def remove_node(self, node: str):
        """Hapus node fisik dari ring"""
        if node not in self.nodes:
            return
            
        self.nodes.remove(node)
        for i in range(self.virtual_replicas):
            key = self._hash(f"{node}:{i}")
            if key in self.ring:
                del self.ring[key]
                # Menghapus dari sorted_keys lebih rumit,
                # cara mudah: temukan index dan hapus
                try:
                    idx = self.sorted_keys.index(key)
                    self.sorted_keys.pop(idx)
                except ValueError:
                    pass # Seharusnya tidak terjadi jika ring konsisten

    def get_nodes(self, key: str, count: int = 3) -> List[str]:
        """
        Dapatkan 'count' node unik yang bertanggung jawab untuk key ini.
        Node pertama adalah Primary, sisanya adalah Replika.
        """
        if not self.ring:
            return []

        # Pastikan kita tidak meminta lebih banyak node daripada yang ada
        unique_node_count = len(self.nodes)
        if count > unique_node_count:
            count = unique_node_count

        found_nodes: List[str] = []
        h = self._hash(key)
        idx = bisect.bisect(self.sorted_keys, h)

        # Mulai cari node searah jarum jam
        while len(found_nodes) < count and len(self.sorted_keys) > 0:
            if idx == len(self.sorted_keys):
                idx = 0 # Wrap around
            
            node_key = self.sorted_keys[idx]
            node = self.ring[node_key]
            
            if node not in found_nodes:
                found_nodes.append(node)
            
            idx += 1
            
        return found_nodes