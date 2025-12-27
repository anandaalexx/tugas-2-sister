import collections
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class LRUCache:
    """
    Implementasi cache LRU (Least Recently Used) yang sederhana.
    
    Menggunakan collections.OrderedDict untuk efisiensi O(1) pada
    operasi get, put, dan delete.
    """
    
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("Kapasitas cache harus lebih besar dari 0")
        self.capacity = capacity
        # OrderedDict menyimpan urutan item dimasukkan.
        self.cache = collections.OrderedDict()
        
        # --- BARU: Metrics Collection ---
        self.hits = 0
        self.misses = 0
        # --- SELESAI BARU ---

    def get(self, key: str) -> Optional[str]:
        """
        Mendapatkan value dari key.
        Jika key ada, tandai sebagai 'most recently used' (paling baru digunakan)
        dengan memindahkannya ke akhir dictionary.
        
        Mengembalikan None jika key tidak ditemukan.
        """
        if key not in self.cache:
            logger.debug(f"CACHE MISS: Key '{key}' not found.")
            # --- BARU: Metrics Collection ---
            self.misses += 1
            # --- SELESAI BARU ---
            return None
        
        # --- BARU: Metrics Collection ---
        self.hits += 1
        # --- SELESAI BARU ---
        
        # Pindahkan key ini ke 'akhir' (paling baru) dari urutan
        self.cache.move_to_end(key)
        logger.debug(f"CACHE HIT: Key '{key}' found.")
        return self.cache[key]

    def put(self, key: str, value: str):
        """
        Menyimpan atau memperbarui key-value pair.
        Ini akan ditandai sebagai 'most recently used'.
        
        Jika cache penuh, item yang 'least recently used' (paling lama)
        akan dihapus.
        """
        if key in self.cache:
            # Jika sudah ada, update value-nya
            self.cache[key] = value
            # Pindahkan ke 'akhir' (paling baru)
            self.cache.move_to_end(key)
        else:
            # Jika key baru
            self.cache[key] = value
            # Cek apakah cache sudah penuh
            if len(self.cache) > self.capacity:
                # 'popitem(last=False)' akan menghapus item PERTAMA (paling lama/LRU)
                # Ini adalah operasi O(1)
                evicted_key, _ = self.cache.popitem(last=False)
                logger.info(f"Cache full. Evicted (LRU): {evicted_key}")
        
        logger.debug(f"CACHE PUT: Key '{key}' set.")

    def delete(self, key: str) -> bool:
        """
        Menghapus key dari cache secara paksa.
        Ini penting untuk proses 'cache invalidation'.
        
        Mengembalikan True jika key ada dan berhasil dihapus.
        """
        if key in self.cache:
            # 'del' pada OrderedDict juga O(1)
            del self.cache[key]
            logger.info(f"CACHE INVALIDATE: Key '{key}' deleted.")
            return True
        return False

    def __len__(self) -> int:
        return len(self.cache)
        
    def __str__(self) -> str:
        return str(list(self.cache.keys()))

    # --- BARU: Metrics Collection ---
    def get_metrics(self) -> dict:
        """Mengembalikan statistik performa cache."""
        total_gets = self.hits + self.misses
        # Menghindari DivisionByZero jika belum ada request
        hit_rate = (self.hits / total_gets * 100) if total_gets > 0 else 0.0
        
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_requests": total_gets,
            "hit_rate": round(hit_rate, 2),
            "current_size": len(self.cache),
            "capacity": self.capacity
        }
        hit_rate = (self.hits / total_gets) if total_gets > 0 else 0.0
        
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_requests": total_gets,
            "hit_rate_percentage": f"{hit_rate:.2%}", # Format jadi persentase
            "current_size": len(self.cache),
            "capacity": self.capacity
        }
    # --- SELESAI BARU ---