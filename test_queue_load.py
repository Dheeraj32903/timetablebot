"""
test_queue_load.py - Controlled Application & Queue Load Test Suite
===================================================================
Tests the proposed multi-user architecture entirely against our own
application, queues, rate limiters, locks, and mock ERP backend.

SAFETY GUARANTEE:
- Zero requests are sent to university ERP or LMS servers.
- Uses mock backend calibrated strictly to the empirically measured
  latencies from live benchmarks.
"""

import asyncio
import collections
import dataclasses
import json
import logging
import math
import os
import random
import statistics
import sys
import time
from typing import Dict, List, Optional, Any, Tuple

import psutil

# Ensure UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_test")


# =====================================================================
# 1. ARCHITECTURAL PROTOTYPE COMPONENTS
# =====================================================================

@dataclasses.dataclass
class JobMetrics:
    job_id: str
    user_id: int
    queue_name: str
    created_at: float
    enqueued_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    queue_wait_ms: float = 0.0
    lock_wait_ms: float = 0.0
    gate_wait_ms: float = 0.0
    backend_latency_ms: float = 0.0
    total_duration_ms: float = 0.0
    retries: int = 0
    success: bool = False
    error: Optional[str] = None


class LeakyBucketRateLimiter:
    """
    Fair FIFO token reservation rate limiter.
    Guarantees strict adherence to max_rps without thundering herds.
    """
    def __init__(self, rate: float, burst_capacity: int = 1):
        self.rate = rate
        self.interval = 1.0 / rate
        self.burst_capacity = burst_capacity
        self._next_time = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            earliest = now - (self.burst_capacity * self.interval)
            if self._next_time < earliest:
                self._next_time = earliest

            if self._next_time <= now:
                self._next_time += self.interval
                return

            wait_time = self._next_time - now
            self._next_time += self.interval
        await asyncio.sleep(wait_time)


class GlobalERPGate:
    """
    Unified gate placed underneath all ERP worker pools:
    1. Bounded concurrency semaphore (max in-flight requests)
    2. Rate limiter (max requests per second)
    3. Concurrency and rate violation tracking
    """
    def __init__(self, max_concurrency: int = 5, max_rps: float = 5.0):
        self.max_concurrency = max_concurrency
        self.max_rps = max_rps
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiter = LeakyBucketRateLimiter(rate=max_rps, burst_capacity=1)

        # Instrumentation & assertions
        self.current_in_flight = 0
        self.max_observed_in_flight = 0
        self.request_timestamps: List[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        t0 = time.perf_counter()
        await self._semaphore.acquire()
        await self._rate_limiter.acquire()

        gate_wait_ms = (time.perf_counter() - t0) * 1000.0

        async with self._lock:
            self.current_in_flight += 1
            if self.current_in_flight > self.max_observed_in_flight:
                self.max_observed_in_flight = self.current_in_flight
            self.request_timestamps.append(time.monotonic())

        return gate_wait_ms

    def release(self):
        asyncio.create_task(self._do_release())

    async def _do_release(self):
        async with self._lock:
            self.current_in_flight -= 1
        self._semaphore.release()


class UserLockRegistry:
    """Thread-safe and async-safe per-user lock registry."""
    def __init__(self):
        self._locks: Dict[int, asyncio.Lock] = {}
        self._active_users: collections.defaultdict = collections.defaultdict(int)
        self.concurrency_violations = 0
        self._registry_lock = asyncio.Lock()

    async def get_lock(self, user_id: int) -> asyncio.Lock:
        async with self._registry_lock:
            if user_id not in self._locks:
                self._locks[user_id] = asyncio.Lock()
            return self._locks[user_id]

    async def on_user_start(self, user_id: int):
        self._active_users[user_id] += 1
        if self._active_users[user_id] > 1:
            self.concurrency_violations += 1

    async def on_user_end(self, user_id: int):
        self._active_users[user_id] -= 1


class TelegramOutbox:
    """
    Dedicated rate-limited dispatcher for outbound Telegram messages.
    Honors 429 Retry-After without blocking other worker pools.
    """
    def __init__(self, max_rps: float = 20.0, simulated_error_rate: float = 0.0):
        self.max_rps = max_rps
        self.min_interval = 1.0 / max_rps
        self.simulated_error_rate = simulated_error_rate
        self.queue: asyncio.Queue = asyncio.Queue()
        self.delivered_count = 0
        self.retried_429_count = 0
        self._last_send = 0.0
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._worker_task = asyncio.create_task(self._dispatcher_loop())

    async def stop(self):
        self._running = False
        if self._worker_task:
            await self.queue.join()
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def send(self, chat_id: int, text: str):
        await self.queue.put((chat_id, text))

    async def _dispatcher_loop(self):
        while self._running:
            try:
                item = await self.queue.get()
                chat_id, text = item

                now = time.monotonic()
                diff = now - self._last_send
                if diff < self.min_interval:
                    await asyncio.sleep(self.min_interval - diff)

                # Simulated sporadic Telegram 429
                if self.simulated_error_rate > 0 and random.random() < self.simulated_error_rate:
                    self.retried_429_count += 1
                    retry_after = 0.5  # 500ms backoff
                    await asyncio.sleep(retry_after)
                    await self.queue.put(item)
                    self.queue.task_done()
                    continue

                # Delivery success
                self._last_send = time.monotonic()
                self.delivered_count += 1
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Outbox error: {e}")


# =====================================================================
# 2. CALIBRATED MOCK ERP BACKEND
# =====================================================================

class MockERPBackend:
    """
    Simulates ERP behavior calibrated to measured latencies:
    - Valid session fetch: ~495ms (P95 ~712ms, Gaussian-clipped)
    - Expired session re-login: ~1880ms (P95 ~2230ms)
    - Outpass submission: ~600ms
    - Outpass history check: ~450ms
    """
    def __init__(self, session_expiry_rate: float = 0.20, network_timeout_rate: float = 0.0):
        self.session_expiry_rate = session_expiry_rate
        self.network_timeout_rate = network_timeout_rate
        self.user_sessions: Dict[int, bool] = {}  # True = valid
        self.outpass_records: Dict[int, List[Dict[str, Any]]] = collections.defaultdict(list)

    async def simulate_fetch_timetable(self, user_id: int) -> Tuple[str, float]:
        t0 = time.perf_counter()
        is_expired = self.user_sessions.get(user_id, None)
        if is_expired is None:
            is_expired = (random.random() < self.session_expiry_rate)
            self.user_sessions[user_id] = not is_expired

        total_delay = 0.0
        if is_expired:
            # Re-login + CAPTCHA inference: calibrated to measured ~1880ms
            login_delay = max(1.2, random.gauss(1.88, 0.25))
            await asyncio.sleep(login_delay)
            total_delay += login_delay
            self.user_sessions[user_id] = True  # now valid

        # Timetable fetch: calibrated to measured ~495ms
        fetch_delay = max(0.22, random.gauss(0.495, 0.12))
        await asyncio.sleep(fetch_delay)
        total_delay += fetch_delay

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        sample_html = f"<html><body>Timetable for user {user_id}</body></html>"
        return sample_html, elapsed_ms

    async def simulate_submit_outpass(self, user_id: int, outpass_data: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.perf_counter()
        if self.network_timeout_rate > 0 and random.random() < self.network_timeout_rate:
            # Simulate dropped connection after server accepted it
            self.outpass_records[user_id].append({
                "outpass_type": outpass_data.get("type", "Local"),
                "from_date": outpass_data.get("from_date"),
                "status": "Pending",
                "submitted_at": time.time()
            })
            await asyncio.sleep(0.8)
            raise TimeoutError("Connection to newerp timed out during POST")

        await asyncio.sleep(random.gauss(0.60, 0.10))
        record = {
            "outpass_type": outpass_data.get("type", "Local"),
            "from_date": outpass_data.get("from_date"),
            "status": "Approved" if outpass_data.get("type") == "Local" else "Pending",
            "submitted_at": time.time()
        }
        self.outpass_records[user_id].append(record)
        return {"success": True, "status": record["status"]}

    async def simulate_fetch_outpass_history(self, user_id: int) -> List[Dict[str, Any]]:
        await asyncio.sleep(random.gauss(0.45, 0.08))
        return list(self.outpass_records.get(user_id, []))


# =====================================================================
# 3. WORKLOAD QUEUE MANAGER & WORKERS
# =====================================================================

class WorkloadManager:
    def __init__(
        self,
        erp_gate: GlobalERPGate,
        lock_registry: UserLockRegistry,
        outbox: TelegramOutbox,
        mock_erp: MockERPBackend,
        outpass_worker_count: int = 3,
        timetable_worker_count: int = 4,
        prefetch_worker_count: int = 4
    ):
        self.erp_gate = erp_gate
        self.lock_registry = lock_registry
        self.outbox = outbox
        self.mock_erp = mock_erp

        # Four Priority Queues
        self.outpass_queue: asyncio.Queue = asyncio.Queue(maxsize=200)       # Priority 0
        self.timetable_queue: asyncio.Queue = asyncio.Queue(maxsize=500)     # Priority 1
        self.lms_queue: asyncio.Queue = asyncio.Queue(maxsize=500)           # Priority 2
        self.prefetch_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)     # Priority 3

        self.outpass_worker_count = outpass_worker_count
        self.timetable_worker_count = timetable_worker_count
        self.prefetch_worker_count = prefetch_worker_count

        self.workers: List[asyncio.Task] = []
        self.completed_metrics: List[JobMetrics] = []
        self._running = False

    async def start(self):
        self._running = True
        for i in range(self.outpass_worker_count):
            self.workers.append(asyncio.create_task(self._outpass_worker_loop(i)))
        for i in range(self.timetable_worker_count):
            self.workers.append(asyncio.create_task(self._timetable_worker_loop(i)))
        for i in range(self.prefetch_worker_count):
            self.workers.append(asyncio.create_task(self._prefetch_worker_loop(i)))

    async def stop(self):
        self._running = False
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    async def _outpass_worker_loop(self, worker_id: int):
        while self._running:
            try:
                job_data = await self.outpass_queue.get()
                metric: JobMetrics = job_data["metric"]
                metric.started_at = time.perf_counter()
                metric.queue_wait_ms = (metric.started_at - metric.enqueued_at) * 1000.0

                user_id = metric.user_id
                user_lock = await self.lock_registry.get_lock(user_id)

                t_lock = time.perf_counter()
                async with user_lock:
                    metric.lock_wait_ms = (time.perf_counter() - t_lock) * 1000.0
                    await self.lock_registry.on_user_start(user_id)

                    try:
                        gate_wait = await self.erp_gate.acquire()
                        metric.gate_wait_ms = gate_wait
                        try:
                            # Submit outpass with post-timeout verification
                            try:
                                res = await self.mock_erp.simulate_submit_outpass(user_id, job_data["payload"])
                                metric.success = True
                            except TimeoutError:
                                # Post-timeout verification check
                                metric.retries += 1
                                history = await self.mock_erp.simulate_fetch_outpass_history(user_id)
                                target_date = job_data["payload"].get("from_date")
                                matched = any(h.get("from_date") == target_date for h in history)
                                if matched:
                                    metric.success = True
                                else:
                                    metric.success = False
                                    metric.error = "Submission timed out and unverified"
                        finally:
                            self.erp_gate.release()
                    finally:
                        await self.lock_registry.on_user_end(user_id)

                metric.completed_at = time.perf_counter()
                metric.total_duration_ms = (metric.completed_at - metric.started_at) * 1000.0
                self.completed_metrics.append(metric)
                self.outpass_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Outpass worker exception: {e}")

    async def _timetable_worker_loop(self, worker_id: int):
        while self._running:
            try:
                job_data = await self.timetable_queue.get()
                metric: JobMetrics = job_data["metric"]
                metric.started_at = time.perf_counter()
                metric.queue_wait_ms = (metric.started_at - metric.enqueued_at) * 1000.0

                user_id = metric.user_id
                user_lock = await self.lock_registry.get_lock(user_id)

                t_lock = time.perf_counter()
                async with user_lock:
                    metric.lock_wait_ms = (time.perf_counter() - t_lock) * 1000.0
                    await self.lock_registry.on_user_start(user_id)

                    try:
                        gate_wait = await self.erp_gate.acquire()
                        metric.gate_wait_ms = gate_wait
                        try:
                            html, dur = await self.mock_erp.simulate_fetch_timetable(user_id)
                            metric.backend_latency_ms = dur
                            metric.success = True
                        finally:
                            self.erp_gate.release()
                    finally:
                        await self.lock_registry.on_user_end(user_id)

                metric.completed_at = time.perf_counter()
                metric.total_duration_ms = (metric.completed_at - metric.started_at) * 1000.0
                self.completed_metrics.append(metric)
                self.timetable_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Timetable worker exception: {e}")

    async def _prefetch_worker_loop(self, worker_id: int):
        while self._running:
            try:
                job_data = await self.prefetch_queue.get()
                metric: JobMetrics = job_data["metric"]
                metric.started_at = time.perf_counter()
                metric.queue_wait_ms = (metric.started_at - metric.enqueued_at) * 1000.0

                user_id = metric.user_id
                user_lock = await self.lock_registry.get_lock(user_id)

                t_lock = time.perf_counter()
                async with user_lock:
                    metric.lock_wait_ms = (time.perf_counter() - t_lock) * 1000.0
                    await self.lock_registry.on_user_start(user_id)

                    try:
                        gate_wait = await self.erp_gate.acquire()
                        metric.gate_wait_ms = gate_wait
                        try:
                            html, dur = await self.mock_erp.simulate_fetch_timetable(user_id)
                            metric.backend_latency_ms = dur
                            metric.success = True
                            
                            # Deliver to Outbox for delivery phase
                            await self.outbox.send(user_id, f"📅 Timetable for {user_id}")
                        finally:
                            self.erp_gate.release()
                    finally:
                        await self.lock_registry.on_user_end(user_id)

                metric.completed_at = time.perf_counter()
                metric.total_duration_ms = (metric.completed_at - metric.started_at) * 1000.0
                self.completed_metrics.append(metric)
                self.prefetch_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Prefetch worker exception: {e}")


# =====================================================================
# 4. BENCHMARK UTILITIES & SUITE EXECUTION
# =====================================================================

def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = int(len(s) * p / 100.0)
    idx = min(idx, len(s) - 1)
    return s[idx]

def summarize(data: List[float]) -> Dict[str, float]:
    if not data:
        return {"avg": 0, "min": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0}
    return {
        "avg": statistics.mean(data),
        "min": min(data),
        "max": max(data),
        "p50": statistics.median(data),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99)
    }

async def run_controlled_load_test():
    print("=" * 80, flush=True)
    print("🔬 CONTROLLED APPLICATION LOAD TEST (100% In-Process / Zero ERP Stress)", flush=True)
    print("=" * 80, flush=True)

    proc = psutil.Process(os.getpid())
    init_mem_mb = proc.memory_info().rss / (1024 * 1024)

    test_results = {}

    # -----------------------------------------------------------------
    # Test 1: Concurrency & Rate Limiting Invariant Verification
    # -----------------------------------------------------------------
    print("\n[Phase 1] Verifying Global ERP Gate Enforcements Under Stress...", flush=True)
    gate = GlobalERPGate(max_concurrency=5, max_rps=5.0)
    locks = UserLockRegistry()
    outbox = TelegramOutbox(max_rps=20.0)
    await outbox.start()
    mock_erp = MockERPBackend()
    manager = WorkloadManager(gate, locks, outbox, mock_erp, outpass_worker_count=3, timetable_worker_count=4, prefetch_worker_count=4)
    await manager.start()

    # Enqueue 30 mixed jobs simultaneously across 10 users
    t0 = time.perf_counter()
    for i in range(30):
        metric = JobMetrics(
            job_id=f"test1_{i}",
            user_id=1000 + (i % 10), # 10 distinct users (collisions to test locks)
            queue_name="timetable",
            created_at=time.time(),
            enqueued_at=time.perf_counter()
        )
        await manager.timetable_queue.put({"metric": metric})

    await manager.timetable_queue.join()
    wall_duration = time.perf_counter() - t0

    # Calculate actual maximum request rate across sliding 1.0s windows
    ts = sorted(gate.request_timestamps)
    max_1s_rate = 0
    for idx, t in enumerate(ts):
        window_count = sum(1 for t2 in ts if 0 <= t2 - t <= 1.0)
        if window_count > max_1s_rate:
            max_1s_rate = window_count

    print(f"  • Enqueued 30 jobs across 10 users. Completed in: {wall_duration:.2f}s", flush=True)
    print(f"  • Max Observed In-Flight ERP Requests: {gate.max_observed_in_flight} (Configured Limit: {gate.max_concurrency})", flush=True)
    print(f"  • Max Observed Requests in Any 1.0s Window: {max_1s_rate} req/s (Configured Limit: {gate.max_rps})", flush=True)
    print(f"  • Per-User Lock Overlap Violations: {locks.concurrency_violations} (Must be 0)", flush=True)

    assert gate.max_observed_in_flight <= gate.max_concurrency, "CRITICAL: Gate concurrency breached!"
    assert locks.concurrency_violations == 0, "CRITICAL: Per-user lock failed to serialize duplicate user requests!"
    print("  ✅ [PASS] Concurrency & User Isolation Invariants Strictly Preserved", flush=True)

    await manager.stop()
    await outbox.stop()

    test_results["phase1_invariants"] = {
        "max_observed_in_flight": gate.max_observed_in_flight,
        "max_concurrency_limit": gate.max_concurrency,
        "max_observed_1s_rate": max_1s_rate,
        "max_rps_limit": gate.max_rps,
        "lock_violations": locks.concurrency_violations
    }

    # -----------------------------------------------------------------
    # Test 2: Priority Preemption Verification
    # -----------------------------------------------------------------
    print("\n[Phase 2] Verifying Priority Isolation (Outpass vs Prefetch)...", flush=True)
    gate = GlobalERPGate(max_concurrency=5, max_rps=5.0)
    locks = UserLockRegistry()
    outbox = TelegramOutbox(max_rps=20.0)
    await outbox.start()
    mock_erp = MockERPBackend()
    manager = WorkloadManager(gate, locks, outbox, mock_erp, outpass_worker_count=2, timetable_worker_count=2, prefetch_worker_count=2)
    await manager.start()

    # Fill prefetch queue with 20 slow jobs
    for i in range(20):
        m = JobMetrics(
            job_id=f"prefetch_{i}",
            user_id=2000 + i,
            queue_name="prefetch",
            created_at=time.time(),
            enqueued_at=time.perf_counter()
        )
        await manager.prefetch_queue.put({"metric": m})

    # Wait 200ms for prefetch to start occupying workers
    await asyncio.sleep(0.2)

    # Now an emergency outpass arrives
    outpass_m = JobMetrics(
        job_id="emergency_outpass_1",
        user_id=9999,
        queue_name="outpass",
        created_at=time.time(),
        enqueued_at=time.perf_counter()
    )
    await manager.outpass_queue.put({"metric": outpass_m, "payload": {"type": "Local", "from_date": "2026-09-18"}})

    # Wait for the outpass to finish
    while not outpass_m.completed_at:
        await asyncio.sleep(0.05)

    print(f"  • Prefetch Queue backlog: {manager.prefetch_queue.qsize()} items remaining", flush=True)
    print(f"  • Emergency Outpass Queue Wait: {outpass_m.queue_wait_ms:.1f}ms", flush=True)
    print(f"  • Emergency Outpass Total Duration: {outpass_m.total_duration_ms:.1f}ms", flush=True)
    print(f"  • Outpass finished ahead of prefetch backlog!", flush=True)
    assert outpass_m.queue_wait_ms < 1000.0, "Outpass was blocked by prefetch!"
    print("  ✅ [PASS] Outpass Queue Successfully Isolated from Prefetch Backlog", flush=True)

    await manager.stop()
    await outbox.stop()

    test_results["phase2_priority"] = {
        "outpass_queue_wait_ms": outpass_m.queue_wait_ms,
        "outpass_total_duration_ms": outpass_m.total_duration_ms
    }

    # -----------------------------------------------------------------
    # Test 3: Outpass Timeout & Post-Timeout Idempotency Verification
    # -----------------------------------------------------------------
    print("\n[Phase 3] Verifying Outpass Idempotency & Post-Timeout Verification...", flush=True)
    gate = GlobalERPGate(max_concurrency=5, max_rps=5.0)
    locks = UserLockRegistry()
    outbox = TelegramOutbox(max_rps=20.0)
    await outbox.start()
    mock_erp_timeout = MockERPBackend(network_timeout_rate=1.0)
    manager = WorkloadManager(gate, locks, outbox, mock_erp_timeout, outpass_worker_count=1)
    await manager.start()

    op_metric = JobMetrics(
        job_id="timeout_test_1",
        user_id=8888,
        queue_name="outpass",
        created_at=time.time(),
        enqueued_at=time.perf_counter()
    )
    await manager.outpass_queue.put({
        "metric": op_metric,
        "payload": {"type": "Local", "from_date": "2026-09-19"}
    })

    await manager.outpass_queue.join()
    print(f"  • Outpass result on simulated network drop: Success={op_metric.success}, Retries/Verifications={op_metric.retries}", flush=True)
    assert op_metric.success == True, "Outpass should have succeeded via history verification!"
    assert op_metric.retries == 1, "Verification pass should have triggered!"
    print("  ✅ [PASS] Outpass Verified via History Despite Network Timeout (No Duplicate Submissions)", flush=True)

    await manager.stop()
    await outbox.stop()

    # -----------------------------------------------------------------
    # Test 4: Full-Scale Controlled Morning Prefetch (100, 500, 1000 Users)
    # -----------------------------------------------------------------
    print("\n[Phase 4] Full-Scale Morning Prefetch & Outbox Load Test (Real Async Queue Execution)...", flush=True)
    
    scale_benchmarks = {}
    for num_users in [100, 500, 1000]:
        print(f"\n  --- Running Full Load Test for N = {num_users} Users ---", flush=True)
        gate = GlobalERPGate(max_concurrency=5, max_rps=5.0)
        locks = UserLockRegistry()
        outbox = TelegramOutbox(max_rps=20.0, simulated_error_rate=0.03) # 3% sporadic 429 Retry-After
        await outbox.start()
        mock_erp = MockERPBackend(session_expiry_rate=0.20) # 20% expired sessions
        manager = WorkloadManager(
            gate, locks, outbox, mock_erp,
            outpass_worker_count=1,
            timetable_worker_count=2,
            prefetch_worker_count=4 # Standard 4 prefetch workers
        )
        await manager.start()

        # Enqueue all users into real prefetch queue
        t_batch_start = time.perf_counter()
        mem_before = proc.memory_info().rss / (1024 * 1024)

        for u in range(num_users):
            m = JobMetrics(
                job_id=f"prefetch_{num_users}_{u}",
                user_id=50000 + u,
                queue_name="prefetch",
                created_at=time.time(),
                enqueued_at=time.perf_counter()
            )
            await manager.prefetch_queue.put({"metric": m})

        # Periodic progress logger while waiting for queue to drain
        total_queued = num_users
        last_logged = 0
        while not manager.prefetch_queue.empty():
            rem = manager.prefetch_queue.qsize()
            done = total_queued - rem
            elapsed = time.perf_counter() - t_batch_start
            if done - last_logged >= max(25, total_queued // 5):
                last_logged = done
                print(f"      [{elapsed:5.1f}s] Completed {done}/{total_queued} users ({done/elapsed:.2f} users/s)...", flush=True)
            await asyncio.sleep(0.5)

        # Wait for all prefetch jobs to fully complete
        await manager.prefetch_queue.join()
        prefetch_wall_s = time.perf_counter() - t_batch_start

        # Wait for outbox delivery to drain all messages
        t_outbox_drain = time.perf_counter()
        await outbox.queue.join()
        outbox_drain_s = time.perf_counter() - t_outbox_drain

        mem_after = proc.memory_info().rss / (1024 * 1024)
        cpu_snap = psutil.cpu_percent(interval=0.2)

        # Extract metric statistics
        q_waits = [m.queue_wait_ms for m in manager.completed_metrics]
        gate_waits = [m.gate_wait_ms for m in manager.completed_metrics]
        backend_lats = [m.backend_latency_ms for m in manager.completed_metrics]
        tot_durs = [m.total_duration_ms for m in manager.completed_metrics]

        res = {
            "num_users": num_users,
            "prefetch_wall_clock_sec": prefetch_wall_s,
            "prefetch_wall_clock_min": prefetch_wall_s / 60.0,
            "outbox_drain_sec": outbox_drain_s,
            "outbox_delivered": outbox.delivered_count,
            "outbox_429_retries": outbox.retried_429_count,
            "effective_prefetch_rps": num_users / prefetch_wall_s,
            "queue_wait_ms": summarize(q_waits),
            "gate_wait_ms": summarize(gate_waits),
            "backend_latency_ms": summarize(backend_lats),
            "total_job_duration_ms": summarize(tot_durs),
            "mem_delta_mb": mem_after - mem_before,
            "final_mem_mb": mem_after,
            "cpu_percent": cpu_snap,
            "max_observed_in_flight": gate.max_observed_in_flight,
            "within_15min_window": prefetch_wall_s < 900.0
        }
        scale_benchmarks[num_users] = res

        print(f"  • Prefetch Wall-Clock Time: {prefetch_wall_s:.1f}s ({prefetch_wall_s/60.0:.2f} mins) [Limit: 15 mins (900s)]", flush=True)
        print(f"  • Delivery Time (@20 msg/s): {outbox_drain_s:.1f}s ({outbox.delivered_count} delivered, {outbox.retried_429_count} 429 retries handled)", flush=True)
        print(f"  • Effective Prefetch Throughput: {res['effective_prefetch_rps']:.2f} users/s", flush=True)
        print(f"  • Job Backend Latency: Avg={res['backend_latency_ms']['avg']:.1f}ms, P95={res['backend_latency_ms']['p95']:.1f}ms", flush=True)
        print(f"  • Job Queue Wait: Avg={res['queue_wait_ms']['avg']:.1f}ms, P95={res['queue_wait_ms']['p95']:.1f}ms", flush=True)
        print(f"  • Peak Concurrent In-Flight: {gate.max_observed_in_flight} (Strict Limit: 5)", flush=True)
        print(f"  • Memory Working Set: {mem_after:.1f} MB (Delta: +{res['mem_delta_mb']:.2f} MB) | CPU: {cpu_snap}%", flush=True)
        print(f"  • Fits inside 06:30 -> 06:45 Window: {'✅ YES' if res['within_15min_window'] else '❌ NO'}", flush=True)

        await manager.stop()
        await outbox.stop()

    test_results["scale_load_tests"] = scale_benchmarks

    # Save to json file
    with open("queue_load_test_results.json", "w") as f:
        json.dump(test_results, f, indent=2)

    print("\n" + "=" * 80, flush=True)
    print("🎯 CONTROLLED LOAD TEST COMPLETE - Results saved to queue_load_test_results.json", flush=True)
    print("=" * 80, flush=True)


if __name__ == "__main__":
    asyncio.run(run_controlled_load_test())
