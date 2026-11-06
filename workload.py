"""
workload.py - Centralized Workload & Job Queue Architecture
===========================================================
Implements a controlled request/job system separating workloads into 4 priority queues:
1. 🔴 Outpass Submissions (Priority 0) - Outpass writes & history verification
2. 🟠 Interactive Timetable (Priority 1) - On-demand student /timetable commands
3. 🟡 LMS Tasks & Submissions (Priority 2) - Coursework, quizzes, deadines
4. 🟢 Morning Timetable Prefetch (Priority 3) - 06:30 AM batch warmup prefetch

Features:
- Global ERP Gate: Bounded semaphore + Leaky Bucket rate limiter protecting ERP.
- Global LMS Gate: Bounded semaphore + rate limiter protecting Moodle LMS.
- UserLockRegistry: Strict per-user asyncio.Lock serializing operations per student.
- Observability: Per-job JobMetrics tracking queue wait, lock wait, gate wait, backend latency, etc.
- TelegramOutbox: Rate-limited message delivery honoring Telegram 429 Retry-After.
"""

import asyncio
import dataclasses
import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import config

logger = logging.getLogger("workload")


# ---------------------------------------------------------------------------
# 1. Observability & Job Timing Metrics
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class JobMetrics:
    job_id: str
    user_id: int
    operation: str
    queue_name: str
    created_at: float
    enqueued_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    queue_wait_ms: float = 0.0
    lock_wait_ms: float = 0.0
    gate_wait_ms: float = 0.0
    captcha_ms: float = 0.0
    login_ms: float = 0.0
    request_latency_ms: float = 0.0
    erp_latency_ms: float = 0.0
    parse_ms: float = 0.0
    total_duration_ms: float = 0.0
    retries: int = 0
    is_warm: bool = True
    success: bool = False
    error: Optional[str] = None

    def log_summary(self):
        mode_str = "Warm Session (1-hop)" if self.is_warm else "Cold Re-Login (CAPTCHA + Login)"
        logger.info(
            "JOB_DONE | id=%s | op=%s | uid=%s | queue=%s | mode=[%s] | wait=%.1fms | lock=%.1fms | "
            "gate=%.1fms | erp=%.1fms | total=%.1fms | success=%s | retries=%d%s",
            self.job_id,
            self.operation,
            self.user_id,
            self.queue_name,
            mode_str,
            self.queue_wait_ms,
            self.lock_wait_ms,
            self.gate_wait_ms,
            self.erp_latency_ms,
            self.total_duration_ms,
            self.success,
            self.retries,
            f" | err={self.error}" if self.error else "",
        )


@dataclasses.dataclass
class Job:
    metrics: JobMetrics
    handler: Callable[..., Coroutine[Any, Any, Any]]
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = dataclasses.field(default_factory=dict)
    future: Optional[asyncio.Future] = None


# ---------------------------------------------------------------------------
# 2. Rate Limiters & Global Concurrency Gates
# ---------------------------------------------------------------------------

class LeakyBucketRateLimiter:
    """
    Fair FIFO token reservation rate limiter.
    Guarantees strict adherence to max_rps without busy-waiting or starvation.
    """
    def __init__(self, rate: float, burst_capacity: int = 1):
        self.rate = max(0.1, rate)
        self.interval = 1.0 / self.rate
        self.burst_capacity = max(1, burst_capacity)
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


class PriorityERPGate:
    """
    Priority Semaphore and Rate Limiter for the ERP backend.
    Priority 0: Outpass (critical hostel passes)
    Priority 1: Interactive Timetable (on-demand student commands)
    Priority 2: LMS
    Priority 3: Prefetch (background batch warmup)

    Ensures Priority 0 and 1 requests always jump ahead of Priority 3 prefetch requests
    when all concurrency slots are saturated.
    """
    def __init__(self, max_concurrency: int = 5, max_rps: float = 5.0):
        self.max_concurrency = max_concurrency
        self.max_rps = max_rps
        self._available_slots = max_concurrency
        self._rate_limiter = LeakyBucketRateLimiter(rate=max_rps, burst_capacity=1)
        self._waiters: List[Tuple[int, float, asyncio.Future]] = []
        self._lock = asyncio.Lock()
        self.current_in_flight = 0
        self.max_observed_in_flight = 0

    async def acquire(self, priority: int = 1) -> float:
        t0 = time.perf_counter()
        fut = None
        async with self._lock:
            if self._available_slots > 0:
                self._available_slots -= 1
            else:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._waiters.append((priority, time.monotonic(), fut))
                self._waiters.sort(key=lambda x: (x[0], x[1]))

        if fut is not None:
            await fut

        try:
            await self._rate_limiter.acquire()
        except BaseException:
            await self.release()
            raise

        gate_wait_ms = (time.perf_counter() - t0) * 1000.0

        async with self._lock:
            self.current_in_flight += 1
            if self.current_in_flight > self.max_observed_in_flight:
                self.max_observed_in_flight = self.current_in_flight

        return gate_wait_ms

    async def release(self):
        async with self._lock:
            self.current_in_flight = max(0, self.current_in_flight - 1)
            while self._waiters:
                _, _, next_fut = self._waiters.pop(0)
                if not next_fut.done():
                    next_fut.set_result(None)
                    return
            self._available_slots += 1

    def priority_slot(self, priority: int = 1):
        """Context manager for acquiring a slot with explicit priority."""
        gate = self
        class _SlotContext:
            def __init__(self):
                self.wait_ms = 0.0
            async def __aenter__(self):
                self.wait_ms = await gate.acquire(priority=priority)
                return self.wait_ms
            async def __aexit__(self, exc_type, exc_val, exc_tb):
                await gate.release()
        return _SlotContext()

    async def __aenter__(self) -> float:
        return await self.acquire(priority=1)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


GlobalERPGate = PriorityERPGate


class GlobalLMSGate:
    """Independent concurrency & rate gate for LMS (Moodle REST) endpoints."""
    def __init__(self, max_concurrency: int = 8, max_rps: float = 8.0):
        self.max_concurrency = max_concurrency
        self.max_rps = max_rps
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiter = LeakyBucketRateLimiter(rate=max_rps, burst_capacity=2)
        self.current_in_flight = 0

    async def acquire(self) -> float:
        t0 = time.perf_counter()
        await self._semaphore.acquire()
        try:
            await self._rate_limiter.acquire()
        except BaseException:
            self._semaphore.release()
            raise
        self.current_in_flight += 1
        return (time.perf_counter() - t0) * 1000.0

    def release(self):
        self.current_in_flight -= 1
        self._semaphore.release()

    async def __aenter__(self) -> float:
        return await self.acquire()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


# ---------------------------------------------------------------------------
# 3. Per-User Async Lock Registry
# ---------------------------------------------------------------------------

class UserLockRegistry:
    """Thread-safe and memory-safe per-user lock registry with auto-pruning."""
    def __init__(self):
        self._locks: Dict[int, asyncio.Lock] = {}
        self._last_accessed: Dict[int, float] = {}
        self._registry_lock = asyncio.Lock()

    async def get_lock(self, user_id: int) -> asyncio.Lock:
        uid = int(user_id)
        async with self._registry_lock:
            now = time.monotonic()
            self._last_accessed[uid] = now
            if len(self._locks) > 100:
                self._prune_unlocked_internal(now)
            if uid not in self._locks:
                self._locks[uid] = asyncio.Lock()
            return self._locks[uid]

    def _prune_unlocked_internal(self, now: Optional[float] = None) -> None:
        """Internal prune for locks that are unlocked and idle."""
        cur = now if now is not None else time.monotonic()
        to_del = [
            uid for uid, lock in self._locks.items()
            if not lock.locked() and (cur - self._last_accessed.get(uid, 0) > 900.0)
        ]
        for uid in to_del:
            self._locks.pop(uid, None)
            self._last_accessed.pop(uid, None)

    async def cleanup_idle_locks(self) -> None:
        """Periodic background sweep for idle unlocked user locks."""
        async with self._registry_lock:
            self._prune_unlocked_internal()



# ---------------------------------------------------------------------------
# 4. Telegram Outbox Delivery Queue (Honors 429 Retry-After)
# ---------------------------------------------------------------------------

class TelegramOutbox:
    """
    Dedicated rate-limited dispatcher for outbound Telegram messages.
    Honors 429 Retry-After without blocking other bot operations.
    """
    def __init__(self, max_rps: float = 20.0):
        self.max_rps = max_rps
        self.min_interval = 1.0 / max_rps
        self.queue: asyncio.Queue = asyncio.Queue()
        self._last_send = 0.0
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self.delivered_count = 0
        self.retried_429_count = 0

    def start(self, bot_instance):
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self._dispatcher_loop(bot_instance))
            logger.info("TelegramOutbox started (Max RPS=%.1f)", self.max_rps)

    async def stop(self):
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
            logger.info("TelegramOutbox stopped (Delivered=%d, Retries=%d)", self.delivered_count, self.retried_429_count)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[Any] = None,
        disable_web_page_preview: Optional[bool] = None,
    ) -> asyncio.Future:
        """Enqueue message for delivery and return a Future that resolves when delivered."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        await self.queue.put({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "disable_web_page_preview": disable_web_page_preview,
            "future": fut,
            "attempts": 0,
        })
        return fut

    async def _dispatcher_loop(self, bot):
        while self._running:
            try:
                item = await self.queue.get()
                now = time.monotonic()
                diff = now - self._last_send
                if diff < self.min_interval:
                    await asyncio.sleep(self.min_interval - diff)

                try:
                    item["attempts"] += 1
                    kwargs = {}
                    if item.get("parse_mode"):
                        kwargs["parse_mode"] = item["parse_mode"]
                    if item.get("reply_markup"):
                        kwargs["reply_markup"] = item["reply_markup"]
                    if item.get("disable_web_page_preview") is not None:
                        kwargs["disable_web_page_preview"] = item["disable_web_page_preview"]

                    msg = await bot.send_message(
                        chat_id=item["chat_id"],
                        text=item["text"],
                        **kwargs,
                    )
                    self._last_send = time.monotonic()
                    self.delivered_count += 1
                    if item.get("future") and not item["future"].done():
                        item["future"].set_result(msg)
                    self.queue.task_done()
                except Exception as exc:
                    exc_str = str(exc).lower()
                    if "retry after" in exc_str or "429" in exc_str:
                        self.retried_429_count += 1
                        # Parse retry duration if available (e.g. "Retry after 5")
                        retry_after = 3.0
                        for part in str(exc).split():
                            if part.isdigit():
                                retry_after = float(part)
                                break
                        logger.warning("Telegram 429 rate limit encountered. Backing off for %.1fs...", retry_after + 0.5)
                        await asyncio.sleep(retry_after + 0.5)
                        # Re-enqueue item
                        await self.queue.put(item)
                        self.queue.task_done()
                    else:
                        logger.error("Failed to deliver Telegram message to %s: %s", item["chat_id"], exc)
                        if item.get("future") and not item["future"].done():
                            item["future"].set_exception(exc)
                        self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in TelegramOutbox loop: %s", e)


# ---------------------------------------------------------------------------
# 5. Workload Manager & Priority Worker Pools
# ---------------------------------------------------------------------------

class WorkloadManager:
    """
    Central request manager for all backend operations:
    - 4 Priority bounded queues
    - Global ERP and LMS gates
    - Per-user session locks
    - Dedicated worker loops
    """
    def __init__(self):
        self.erp_gate = GlobalERPGate(
            max_concurrency=getattr(config, "GLOBAL_ERP_MAX_CONCURRENCY", 5),
            max_rps=getattr(config, "GLOBAL_ERP_MAX_RPS", 5.0),
        )
        self.lms_gate = GlobalLMSGate(
            max_concurrency=getattr(config, "GLOBAL_LMS_MAX_CONCURRENCY", 8),
            max_rps=getattr(config, "GLOBAL_LMS_MAX_RPS", 8.0),
        )
        self.user_locks = UserLockRegistry()
        self.outbox = TelegramOutbox(
            max_rps=getattr(config, "TELEGRAM_DELIVERY_MAX_RPS", 20.0),
        )

        # Four Priority Bounded Queues
        self.outpass_queue: asyncio.Queue = asyncio.Queue(
            maxsize=getattr(config, "OUTPASS_QUEUE_MAXSIZE", 200)
        )
        self.timetable_queue: asyncio.Queue = asyncio.Queue(
            maxsize=getattr(config, "TIMETABLE_QUEUE_MAXSIZE", 500)
        )
        self.lms_queue: asyncio.Queue = asyncio.Queue(
            maxsize=getattr(config, "LMS_QUEUE_MAXSIZE", 500)
        )
        self.prefetch_queue: asyncio.Queue = asyncio.Queue(
            maxsize=getattr(config, "PREFETCH_QUEUE_MAXSIZE", 2000)
        )

        self._running = False
        self._workers: List[asyncio.Task] = []
        self._job_counter = 0
        self._in_flight_requests: Dict[Tuple[int, str], asyncio.Future] = {}
        self._in_flight_lock = asyncio.Lock()

    def _next_job_id(self, prefix: str) -> str:
        self._job_counter += 1
        return f"{prefix}_{int(time.time())}_{self._job_counter}"

    async def start(self, bot_instance=None):
        if self._running:
            return
        self._running = True

        if bot_instance:
            self.outbox.start(bot_instance)

        # Spawn worker pools according to configuration
        outpass_w = getattr(config, "OUTPASS_WORKERS", 3)
        tt_w = getattr(config, "TIMETABLE_WORKERS", 4)
        lms_w = getattr(config, "LMS_WORKERS", 4)
        prefetch_w = getattr(config, "PREFETCH_WORKERS", 4)

        for i in range(outpass_w):
            self._workers.append(asyncio.create_task(self._worker_loop(self.outpass_queue, f"OutpassWorker-{i}")))
        for i in range(tt_w):
            self._workers.append(asyncio.create_task(self._worker_loop(self.timetable_queue, f"TimetableWorker-{i}")))
        for i in range(lms_w):
            self._workers.append(asyncio.create_task(self._worker_loop(self.lms_queue, f"LMSWorker-{i}")))
        for i in range(prefetch_w):
            self._workers.append(asyncio.create_task(self._worker_loop(self.prefetch_queue, f"PrefetchWorker-{i}")))

        logger.info(
            "WorkloadManager started with %d workers (Outpass=%d, Timetable=%d, LMS=%d, Prefetch=%d)",
            len(self._workers),
            outpass_w,
            tt_w,
            lms_w,
            prefetch_w,
        )

    async def stop(self):
        if not self._running:
            return
        self._running = False
        logger.info("Stopping WorkloadManager worker pools...")

        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        await self.outbox.stop()
        logger.info("WorkloadManager stopped.")

    async def _worker_loop(self, queue: asyncio.Queue, worker_name: str):
        while self._running:
            try:
                job: Job = await queue.get()
                m = job.metrics
                m.started_at = time.perf_counter()
                m.queue_wait_ms = (m.started_at - m.enqueued_at) * 1000.0

                user_id = m.user_id
                user_lock = await self.user_locks.get_lock(user_id)

                t_lock = time.perf_counter()
                async with user_lock:
                    m.lock_wait_ms = (time.perf_counter() - t_lock) * 1000.0

                    try:
                        import inspect
                        sig = inspect.signature(job.handler)
                        call_kwargs = dict(job.kwargs)
                        if "metric" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                            call_kwargs["metric"] = m
                        if "user_id" in sig.parameters:
                            param_names = list(sig.parameters.keys())
                            user_id_idx = param_names.index("user_id")
                            if user_id_idx >= len(job.args):
                                if "user_id" not in call_kwargs or call_kwargs["user_id"] is None:
                                    call_kwargs["user_id"] = user_id
                        elif "user_id" in call_kwargs and not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                            call_kwargs.pop("user_id", None)

                        queue_priority = 1
                        if queue is self.outpass_queue:
                            queue_priority = 0
                        elif queue is self.timetable_queue:
                            queue_priority = 1
                        elif queue is self.lms_queue:
                            queue_priority = 2
                        elif queue is self.prefetch_queue:
                            queue_priority = 3

                        if "priority" in sig.parameters and "priority" not in call_kwargs:
                            call_kwargs["priority"] = queue_priority

                        res = await job.handler(*job.args, **call_kwargs)
                        m.success = True
                        if job.future and not job.future.done():
                            job.future.set_result(res)
                    except Exception as exc:
                        m.success = False
                        m.error = str(exc)
                        if job.future and not job.future.done():
                            job.future.set_exception(exc)
                    finally:
                        m.completed_at = time.perf_counter()
                        m.total_duration_ms = (m.completed_at - m.started_at) * 1000.0
                        m.log_summary()
                        queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in %s: %s", worker_name, e)

    # -----------------------------------------------------------------------
    # Public Job Submission APIs
    # -----------------------------------------------------------------------

    async def submit_job(
        self,
        queue: asyncio.Queue,
        *args,
        **kwargs,
    ) -> Any:
        """
        Submit a job to a specific queue and await its result asynchronously.
        Supports both positional and keyword invocations without argument conflicts:
            submit_job(queue, user_id, operation, queue_name, handler, *args, **kwargs)
            submit_job(queue, user_id=..., operation=..., queue_name=..., handler=..., **kwargs)
        """
        arg_list = list(args)

        # 1. user_id
        if arg_list:
            user_id = arg_list.pop(0)
        elif "user_id" in kwargs:
            user_id = kwargs.pop("user_id")
        else:
            raise TypeError("submit_job() missing required argument: 'user_id'")

        # 2. operation
        if arg_list:
            operation = arg_list.pop(0)
        elif "operation" in kwargs:
            operation = kwargs.pop("operation")
        else:
            operation = "unknown"

        # 3. queue_name
        if arg_list:
            queue_name = arg_list.pop(0)
        elif "queue_name" in kwargs:
            queue_name = kwargs.pop("queue_name")
        else:
            queue_name = "general"

        # 4. handler
        if arg_list:
            handler = arg_list.pop(0)
        elif "handler" in kwargs:
            handler = kwargs.pop("handler")
        else:
            raise TypeError("submit_job() missing required argument: 'handler'")

        handler_args = tuple(arg_list)
        handler_kwargs = kwargs

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        job_id = self._next_job_id(str(operation))
        now_perf = time.perf_counter()

        metrics = JobMetrics(
            job_id=job_id,
            user_id=int(user_id),
            operation=str(operation),
            queue_name=str(queue_name),
            created_at=time.time(),
            enqueued_at=now_perf,
        )

        job = Job(
            metrics=metrics,
            handler=handler,
            args=handler_args,
            kwargs=handler_kwargs,
            future=fut,
        )

        try:
            # Non-blocking enqueue if queue has capacity, or wait
            await asyncio.wait_for(queue.put(job), timeout=5.0)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Server is busy: {queue_name} queue is full. Please try again in a moment.")

        return await fut

    async def submit_coalesced_job(
        self,
        command_key: Tuple[int, str],
        queue: asyncio.Queue,
        user_id: int,
        operation: str,
        queue_name: str,
        handler: Callable[..., Coroutine[Any, Any, Any]],
        *args,
        **kwargs,
    ) -> Any:
        """
        Coalesce identical in-flight real-time requests for the same user and command.
        If a user clicks /timetable multiple times while one is already in flight,
        both requests attach to the same Future and receive the real-time result
        without double-hitting the ERP.
        """
        async with self._in_flight_lock:
            if command_key in self._in_flight_requests:
                existing_fut = self._in_flight_requests[command_key]
                if not existing_fut.done():
                    logger.info("COALESCE_HIT | key=%s - joined in-flight request", command_key)
                    return await existing_fut

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._in_flight_requests[command_key] = fut

        try:
            result = await self.submit_job(queue, user_id, operation, queue_name, handler, *args, **kwargs)
            if not fut.done():
                fut.set_result(result)
            return result
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            async with self._in_flight_lock:
                self._in_flight_requests.pop(command_key, None)


# Global singleton instance
workload_manager = WorkloadManager()
