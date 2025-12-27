import asyncio
import logging
import random
from typing import Dict, Optional, List, Any

from src.utils.message_client import MessageClient 
from src.utils.redis_client import RedisClient # Kita butuh ini untuk deadlock detection

logging.basicConfig(level="INFO")
logger = logging.getLogger(__name__)

class RaftNode:
    
    # --- PERBAIKAN: Tambahkan 'client: MessageClient' sebagai argumen ---
    def __init__(self, node_id: str, peers: Dict[str, str], redis_client: RedisClient, client: MessageClient):
        self.node_id = node_id
        self.peers = peers
        # --- PERBAIKAN: Gunakan client yang di-pass, jangan buat baru ---
        self.client = client
        
        # === State Machine (Logika Aplikasi Anda) ===
        # Diambil dari LockManager
        self.locks: Dict[str, Any] = {}
        # Kita butuh Redis HANYA untuk deadlock detection graph
        self.redis = redis_client 
        self._deadlock_task: Optional[asyncio.Task] = None
        self.config = {} # Ditambahkan untuk self.config.get() di _deadlock_loop

        # === Persistent State (Raft) ===
        self.current_term = 0
        self.voted_for: Optional[str] = None
        # Log sekarang berisi {"term": ..., "command": "..."}
        # Tambahkan dummy entry di index 0 untuk mempermudah (index 1-based)
        self.log: List[Dict[str, Any]] = [{"term": 0, "command": None}] 

        # === Volatile State (Raft) ===
        self.commit_index = 0
        self.last_applied = 0 # Index log terakhir yang di-apply ke state machine
        self.state = "follower"
        
        # --- PERBAIKAN ADA DI SINI ---
        # Definisikan interval heartbeat (misal: 50ms)
        self._heartbeat_interval_s = 0.05 
        # --- SELESAI PERBAIKAN ---

        self._reset_election_timer() # Panggil ini untuk inisialisasi timer

        # === Volatile State (Leader only) ===
        self.next_index: Dict[str, int] = {}
        self.match_index: Dict[str, int] = {}
        
        # Untuk client request, melacak kapan request selesai di-commit
        self.pending_requests: Dict[int, asyncio.Future] = {} # Map log_index -> Future

    # ... (Fungsi _get_random_election_timeout dan _reset_election_timer sama) ...
    def _get_random_election_timeout(self) -> float:
        return random.uniform(150, 300) / 1000.0

    def _reset_election_timer(self):
        self._last_rpc_time = asyncio.get_event_loop().time()
        self._current_election_timeout_s = self._get_random_election_timeout()

    # --- Transisi State (Sama seperti sebelumnya) ---

    async def _transition_to_follower(self, term: int):
        logger.info(f"[{self.node_id}] Transitioning to FOLLOWER (term {term})")
        self.state = "follower"
        self.current_term = term
        self.voted_for = None
        self._reset_election_timer()
        # Hentikan deadlock task jika sedang berjalan
        if self._deadlock_task and not self._deadlock_task.done():
            self._deadlock_task.cancel()

    async def _transition_to_candidate(self):
        self.state = "candidate"
        self.current_term += 1
        self.voted_for = self.node_id
        self._votes_received = {self.node_id}
        self._reset_election_timer()
        logger.info(f"[{self.node_id}] Transitioning to CANDIDATE, starting election for term {self.current_term}")
        
        await self._start_election()

    async def _transition_to_leader(self):
        if self.state != "candidate":
            return 
        logger.info(f"[{self.node_id}] Transitioning to LEADER for term {self.current_term}")
        self.state = "leader"
        
        # Re-inisialisasi next_index dan match_index
        last_log_index = len(self.log) - 1 # Index 0 adalah dummy
        self.next_index = {peer_id: last_log_index + 1 for peer_id in self.peers}
        self.match_index = {peer_id: 0 for peer_id in self.peers}
        
        # Mulai deadlock detection loop (HANYA LEADER)
        self._deadlock_task = asyncio.create_task(self._deadlock_loop())

        # Segera kirim log (awalnya heartbeat)
        await self._replicate_log_to_peers()

    # --- Logika Election (Diperbarui dengan log info) ---

    async def _start_election(self):
        last_log_index = len(self.log) - 1
        last_log_term = self.log[last_log_index]["term"]
        
        payload = {
            "term": self.current_term,
            "candidateId": self.node_id,
            "lastLogIndex": last_log_index,
            "lastLogTerm": last_log_term
        }
        # ... (Sisa logic _start_election dan _handle_vote_response sama) ...
        tasks = []
        for peer_id, peer_url in self.peers.items():
            if peer_id != self.node_id:
                task = asyncio.create_task(self.client.send(peer_url, "request_vote", payload))
                tasks.append((peer_id, task))

        for peer_id, task in tasks:
            response = await task
            if response:
                await self._handle_vote_response(response) # Asumsi _handle_vote_response ada
                if self.state != "candidate":
                    break # Berhenti jika kita bukan lagi candidate

    async def _handle_vote_response(self, response: Dict):
        if response.get("term", 0) > self.current_term:
            await self._transition_to_follower(response["term"])
            return

        if self.state == "candidate" and response.get("voteGranted"):
            # Asumsi response berisi 'nodeId' dari voter
            self._votes_received.add(response.get("nodeId", "unknown")) 
            
            majority = ((len(self.peers) + 1) // 2) + 1 # +1 untuk diri sendiri
            if len(self._votes_received) >= majority:
                await self._transition_to_leader()

    # --- Logika Replikasi Log (BARU & PENTING) ---

    async def _replicate_log_to_peers(self):
        """Leader: Kirim AppendEntries ke semua peer"""
        if self.state != "leader":
            return

        for peer_id, peer_url in self.peers.items():
            asyncio.create_task(self._replicate_log_to_peer(peer_id, peer_url))
    
    async def _replicate_log_to_peer(self, peer_id: str, peer_url: str):
        """Leader: Kirim log entry ke satu peer"""
        if self.state != "leader":
            return
            
        next_idx = self.next_index[peer_id]
        
        # Cek apakah log kita cukup panjang untuk dikirim
        if len(self.log) < next_idx:
            # Kita tidak punya log baru, kirim heartbeat
            prev_log_index = len(self.log) - 1
        else:
            # Kita punya log baru untuk dikirim
            prev_log_index = next_idx - 1

        prev_log_term = self.log[prev_log_index]["term"]
        entries = self.log[next_idx:]
        
        payload = {
            "term": self.current_term,
            "leaderId": self.node_id,
            "prevLogIndex": prev_log_index,
            "prevLogTerm": prev_log_term,
            "entries": entries, # Bisa kosong (heartbeat) atau berisi command
            "leaderCommit": self.commit_index
        }
        
        response = await self.client.send(peer_url, "append_entries", payload)
        
        if response:
            if response.get("term", 0) > self.current_term:
                await self._transition_to_follower(response["term"])
                return

            if self.state == "leader":
                if response.get("success"):
                    # Follower berhasil append
                    new_match_index = prev_log_index + len(entries)
                    self.match_index[peer_id] = new_match_index
                    self.next_index[peer_id] = new_match_index + 1
                    
                    # Cek apakah kita bisa advance commit index
                    await self._advance_commit_index()
                else:
                    # Follower gagal (log mismatch), decrement next_index dan coba lagi
                    self.next_index[peer_id] = max(1, next_idx - 1)
                    # Kita bisa optimis kirim lagi, tapi loop utama akan handle
        else:
            logger.warning(f"[{self.node_id}] No response from {peer_id} on AppendEntries")

    async def _advance_commit_index(self):
        """Leader: Cek apakah commit index bisa dinaikkan"""
        if self.state != "leader":
            return
            
        majority = ((len(self.peers) + 1) // 2) + 1 # +1 untuk diri sendiri
        
        # Loop dari entry terbaru ke commit_index lama
        for n in range(len(self.log) - 1, self.commit_index, -1):
            # Hanya commit entry dari term kita saat ini
            if self.log[n]["term"] == self.current_term:
                matches = 1 # 1 untuk diri sendiri
                for peer_id in self.peers:
                    if self.match_index[peer_id] >= n:
                        matches += 1
                
                if matches >= majority:
                    # Mayoritas telah mereplikasi sampai index n!
                    self.commit_index = n
                    logger.debug(f"[{self.node_id}] Advanced commit_index to {self.commit_index}")
                    break # Kita hanya advance ke N terbaru

    async def _apply_committed_entries(self):
        """All Nodes: Apply log entries yang sudah di-commit"""
        while self.commit_index > self.last_applied:
            self.last_applied += 1
            log_entry = self.log[self.last_applied]
            command = log_entry.get("command")
            
            logger.info(f"[{self.node_id}] Applying committed log {self.last_applied}: {command}")
            
            # Panggil state machine (logika lock)
            result = await self._apply_command(command)
            
            # Jika ada client future yang menunggu, beri hasilnya
            if self.last_applied in self.pending_requests:
                future = self.pending_requests.pop(self.last_applied)
                future.set_result(result)

    # --- RPC Handlers (Diperbarui) ---

    async def handle_request_vote(self, request: Dict):
        term = request.get("term", 0)
        candidate_id = request.get("candidateId")
        
        if term < self.current_term:
            return {"term": self.current_term, "voteGranted": False, "nodeId": self.node_id}

        if term > self.current_term:
            await self._transition_to_follower(term)
        
        # Cek kelengkapan log kandidat
        last_log_index = len(self.log) - 1
        last_log_term = self.log[last_log_index]["term"]
        candidate_last_log_index = request.get("lastLogIndex", 0)
        candidate_last_log_term = request.get("lastLogTerm", 0)

        is_log_ok = (candidate_last_log_term > last_log_term) or \
                    (candidate_last_log_term == last_log_term and \
                     candidate_last_log_index >= last_log_index)
        
        vote_granted = False
        if (self.voted_for is None or self.voted_for == candidate_id) and is_log_ok:
            self.voted_for = candidate_id
            vote_granted = True
            self._reset_election_timer()
            logger.info(f"[{self.node_id}] GRANTED vote for {candidate_id}")
        else:
            logger.info(f"[{self.node_id}] REJECTED vote for {candidate_id} (log_ok={is_log_ok}, voted_for={self.voted_for})")

        return {"term": self.current_term, "voteGranted": vote_granted, "nodeId": self.node_id}

    async def handle_append_entries(self, request: Dict):
        term = request.get("term", 0)
        leader_id = request.get("leaderId")

        if term < self.current_term:
            return {"term": self.current_term, "success": False}

        self._reset_election_timer()
        if term > self.current_term or self.state == "candidate":
            await self._transition_to_follower(term)
        
        # 2. Cek konsistensi log
        prev_log_index = request.get("prevLogIndex", 0)
        prev_log_term = request.get("prevLogTerm", 0)

        if len(self.log) - 1 < prev_log_index or \
           self.log[prev_log_index]["term"] != prev_log_term:
            logger.warning(f"[{self.node_id}] Log mismatch. Local: {self.log[prev_log_index] if len(self.log) > prev_log_index else 'OOB'} vs Remote: {prev_log_term}")
            return {"term": self.current_term, "success": False}

        # 3. Hapus log yang konflik dan tambahkan entry baru
        entries = request.get("entries", [])
        if entries:
            # Hapus log yang ada *setelah* prevLogIndex
            self.log = self.log[:prev_log_index + 1]
            # Tambahkan entries baru
            self.log.extend(entries)
            logger.info(f"[{self.node_id}] Appended {len(entries)} entries. New log length: {len(self.log)}")

        # 5. Update commit_index
        leader_commit = request.get("leaderCommit", 0)
        if leader_commit > self.commit_index:
            self.commit_index = min(leader_commit, len(self.log) - 1)
            
        return {"term": self.current_term, "success": True}

    # --- Client Propose (BARU) ---

    async def propose(self, command: str) -> Dict[str, Any]:
        """Dipanggil oleh HTTP handler saat client request (lock/unlock)"""
        if self.state != "leader":
            # (Nanti kita bisa forward request ke leader)
            return {"success": False, "message": "Not the leader. Try another node."}

        log_entry = {"term": self.current_term, "command": command}
        self.log.append(log_entry)
        new_log_index = len(self.log) - 1
        
        logger.info(f"[{self.node_id}] Proposed new command at log {new_log_index}: {command}")

        # Siapkan future untuk menunggu hasil apply
        future = asyncio.Future()
        self.pending_requests[new_log_index] = future
        
        # Segera trigger replikasi (jangan tunggu main loop)
        await self._replicate_log_to_peers()
        
        try:
            # Tunggu sampai command di-apply (via _apply_committed_entries)
            result = await asyncio.wait_for(future, timeout=5.0)
            return {"success": True, "data": result}
        except asyncio.TimeoutError:
            logger.warning(f"[{self.node_id}] Timeout waiting for commit log {new_log_index}")
            self.pending_requests.pop(new_log_index, None)
            return {"success": False, "message": "Timeout waiting for consensus."}
        except Exception as e:
            logger.error(f"[{self.node_id}] Error waiting for future: {e}")
            return {"success": False, "message": str(e)}

    # --- State Machine (Logika Lock Anda, dipindah dari LockManager) ---

    async def _apply_command(self, cmd: str) -> Dict[str, Any]:
        """
        Menerapkan command ke state machine (locks).
        Diambil dari LockManager Anda, tapi diubah untuk MENGEMBALIKAN hasil.
        """
        if cmd is None: # Dummy log
            return {"status": "noop"}
            
        parts = cmd.split(":")
        action = parts[0]
        result: Dict[str, Any] = {"status": "unknown_command"}

        try:
            if action == "LOCK":
                _, resource, owner, mode = parts
                r = self.locks.setdefault(resource, {"owners": set(), "mode": None})
                current_mode = r["mode"]
                owners = r["owners"]

                if current_mode is None:
                    r["mode"] = mode
                    owners.add(owner)
                    await self._clear_wait(owner) # Hapus dari deadlock graph
                    
                    # Save lock to Redis for API endpoint
                    import json
                    lock_data = {"owners": list(owners), "mode": mode}
                    await self.redis.set(f"lock:{resource}", json.dumps(lock_data))
                    
                    logger.info(f"[{self.node_id}] LOCK GRANTED {resource} by {owner} ({mode})")
                    result = {"status": "granted", "resource": resource, "owner": owner}
                
                elif current_mode == "S" and mode == "S":
                    owners.add(owner)
                    await self._clear_wait(owner)
                    
                    # Update lock in Redis
                    import json
                    lock_data = {"owners": list(owners), "mode": "S"}
                    await self.redis.set(f"lock:{resource}", json.dumps(lock_data))
                    
                    logger.info(f"[{self.node_id}] LOCK GRANTED (Shared) {resource} by {owner}")
                    result = {"status": "granted_shared", "resource": resource, "owner": owner}
                
                else: # Konflik
                    logger.info(f"[{self.node_id}] LOCK DENIED {resource} for {owner} - conflict with {owners}")
                    await self._record_wait(owner, set(owners)) # Tambah ke deadlock graph
                    result = {"status": "denied_conflict", "waiting_on": list(owners)}

            elif action == "UNLOCK":
                _, resource, owner = parts[:3]
                if resource in self.locks and owner in self.locks[resource]["owners"]:
                    self.locks[resource]["owners"].remove(owner)
                    if not self.locks[resource]["owners"]:
                        self.locks[resource]["mode"] = None
                        # Delete lock from Redis
                        await self.redis.delete(f"lock:{resource}")
                    else:
                        # Update lock in Redis with remaining owners
                        import json
                        lock_data = {"owners": list(self.locks[resource]["owners"]), "mode": self.locks[resource]["mode"]}
                        await self.redis.set(f"lock:{resource}", json.dumps(lock_data))
                    
                    await self._clear_wait(owner) # Hapus dari deadlock graph
                    logger.info(f"[{self.node_id}] UNLOCK {resource} by {owner}")
                    result = {"status": "unlocked", "resource": resource}
                else:
                    logger.warning(f"[{self.node_id}] UNLOCK {resource} failed - owner mismatch")
                    result = {"status": "unlock_failed", "resource": resource}
            
            elif action == "ABORT": # Dari deadlock detection
                _, victim = parts[:2]
                await self._clear_wait(victim)
                logger.info(f"[{self.node_id}] ABORT received for {victim}")
                result = {"status": "aborted", "victim": victim}
        
        except Exception as e:
            logger.error(f"[{self.node_id}] Error applying command '{cmd}': {e}")
            result = {"status": "error", "message": str(e)}

        return result

    # --- Logika Deadlock (Dipindah dari LockManager/ClusterNode) ---
    
    async def _record_wait(self, waiter: str, holders: set):
        if holders:
            key = f"waits:{waiter}"
            await self.redis.sadd(key, *list(holders))
            logger.debug(f"[{self.node_id}] Recorded wait {waiter} -> {holders}")

    async def _clear_wait(self, waiter: str):
        key = f"waits:{waiter}"
        await self.redis.delete(key)

    async def _get_all_waits(self) -> Dict[str, List[str]]:
        ret = {}
        keys = await self.redis.keys("waits:*")
        for k in keys:
            waiter = k.split(":", 1)[1]
            members = await self.redis.smembers(k)
            ret[waiter] = list(members)
        return ret

    async def _detect_deadlock(self) -> Optional[List[str]]:
        graph = await self._get_all_waits()
        visited = set()
        stack = set()

        def dfs(node, path):
            visited.add(node)
            stack.add(node)
            for nbr in graph.get(node, []):
                if nbr not in visited:
                    res = dfs(nbr, path + [nbr])
                    if res: return res
                elif nbr in stack: # Siklus ditemukan
                    try:
                        idx = path.index(nbr)
                        return path[idx:] + [nbr]
                    except ValueError:
                        return [nbr, nbr]
            stack.remove(node)
            return None

        for node in list(graph.keys()):
            if node not in visited:
                cycle = dfs(node, [node])
                if cycle:
                    return cycle
        return None

    async def _break_deadlock(self, cycle: List[str]):
        victim = sorted(set(cycle))[-1] # Pilih korban (misal: ID terbesar)
        logger.warning(f"[{self.node_id}] Deadlock detected in {cycle}. Aborting victim {victim}.")
        
        # Buat command ABORT dan replikasikan via Raft
        abort_command = f"ABORT:{victim}"
        # Kita panggil propose, tapi tidak perlu await hasilnya (fire-and-forget)
        # Kita tidak bisa await di sini karena akan deadlock (propose menunggu _apply)
        asyncio.create_task(self.propose(abort_command))

    async def _deadlock_loop(self):
        """Hanya leader yang melakukan deteksi deadlock."""
        interval = self.config.get("deadlock_check_interval", 5) # Asumsi config
        try:
            while self.state == "leader":
                await asyncio.sleep(interval)
                cycle = await self._detect_deadlock()
                if cycle:
                    await self._break_deadlock(cycle)
        except asyncio.CancelledError:
            logger.info(f"[{self.node_id}] Deadlock loop cancelled.")
        except Exception as e:
            logger.error(f"[{self.node_id}] Deadlock loop crashed: {e}")

    # --- Main Loop (Diperbarui) ---

    async def run(self):
        logger.info(f"[{self.node_id}] Starting RaftNode... initial state: {self.state}")
        
        while True:
            try:
                # 1. Terapkan command yang sudah di-commit
                await self._apply_committed_entries()

                # 2. Logika berdasarkan state
                if self.state == "follower" or self.state == "candidate":
                    time_elapsed = asyncio.get_event_loop().time() - self._last_rpc_time
                    if time_elapsed > self._current_election_timeout_s:
                        logger.warning(f"[{self.node_id}] Election timeout!")
                        await self._transition_to_candidate()
                
                elif self.state == "leader":
                    # Leader mengirim log/heartbeat
                    await self._replicate_log_to_peers()
                    await asyncio.sleep(self._heartbeat_interval_s) # Heartbeat interval

                await asyncio.sleep(0.01) # Check loop
                
            except Exception as e:
                logger.error(f"[{self.node_id}] CRITICAL ERROR in main loop: {e}", exc_info=True)
                # Dalam production, mungkin lebih baik transisi ke follower
                await self._transition_to_follower(self.current_term)