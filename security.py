import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)

# --- Configuration Constants ---
BURST_WINDOW_SECONDS = 3.0
BURST_MAX_REQUESTS = 4

SUSTAINED_WINDOW_SECONDS = 60.0
SUSTAINED_MAX_REQUESTS = 20

CALLBACK_WINDOW_SECONDS = 5.0
CALLBACK_MAX_CLICKS = 15

COOLDOWN_PENALTY_SECONDS = 20.0
MAX_INPUT_LENGTH = 300

MAX_CONCURRENT_ERP_REQUESTS = 5
MAX_CONCURRENT_LMS_REQUESTS = 10

MAX_FAILED_LOGINS = 5
LOGIN_LOCKOUT_SECONDS = 900.0  # 15 minutes


class RateLimiter:
    """
    Sliding-window rate limiter with burst, sustained, and UI callback thresholds.
    Includes automated progressive cooldown and silent drop to prevent outbound flood amplification.
    """

    def __init__(self):
        self._message_timestamps: dict[int, deque[float]] = defaultdict(deque)
        self._callback_timestamps: dict[int, deque[float]] = defaultdict(deque)
        self._cooldowns: dict[int, float] = {}  # user_id -> cooldown_end_timestamp
        self._warned_cooldowns: set[int] = set()  # Tracks if user received the initial cooldown warning

    def _cleanup_old_timestamps(self, queue: deque[float], window: float, now: float) -> None:
        cutoff = now - window
        while queue and queue[0] < cutoff:
            queue.popleft()

    def check_rate_limit(self, update: Update) -> Tuple[bool, bool, float]:
        """
        Check if an update exceeds rate limits.
        Returns: (is_allowed, is_first_warning, remaining_cooldown_seconds)
        """
        user = update.effective_user
        if not user:
            return True, False, 0.0

        uid = user.id
        now = time.time()

        # 1. Check if currently in cooldown
        cooldown_end = self._cooldowns.get(uid, 0.0)
        if now < cooldown_end:
            # User is in active cooldown
            remaining = cooldown_end - now
            is_first_warn = uid not in self._warned_cooldowns
            if is_first_warn:
                self._warned_cooldowns.add(uid)
            return False, is_first_warn, remaining

        # Cooldown expired, clean up warning flag
        if uid in self._warned_cooldowns:
            self._warned_cooldowns.discard(uid)
            self._cooldowns.pop(uid, None)

        # 2. Check Callback Queries (Inline button taps)
        if update.callback_query:
            cb_queue = self._callback_timestamps[uid]
            self._cleanup_old_timestamps(cb_queue, CALLBACK_WINDOW_SECONDS, now)
            if len(cb_queue) >= CALLBACK_MAX_CLICKS:
                # Trigger cooldown
                self._cooldowns[uid] = now + COOLDOWN_PENALTY_SECONDS
                self._warned_cooldowns.add(uid)
                return False, True, COOLDOWN_PENALTY_SECONDS
            cb_queue.append(now)
            return True, False, 0.0

        # 3. Check Messages & Commands
        msg_queue = self._message_timestamps[uid]
        self._cleanup_old_timestamps(msg_queue, SUSTAINED_WINDOW_SECONDS, now)

        # Count requests in burst window
        burst_cutoff = now - BURST_WINDOW_SECONDS
        burst_count = sum(1 for t in msg_queue if t >= burst_cutoff)

        if burst_count >= BURST_MAX_REQUESTS or len(msg_queue) >= SUSTAINED_MAX_REQUESTS:
            # Trigger cooldown penalty
            self._cooldowns[uid] = now + COOLDOWN_PENALTY_SECONDS
            self._warned_cooldowns.add(uid)
            logger.warning(
                "Rate limit triggered for user %s (burst: %d, sustained: %d). Imposing %ds cooldown.",
                uid,
                burst_count,
                len(msg_queue),
                COOLDOWN_PENALTY_SECONDS,
            )
            return False, True, COOLDOWN_PENALTY_SECONDS

        msg_queue.append(now)
        return True, False, 0.0

    def cleanup_inactive_users(self) -> None:
        """Periodically prune memory for users who haven't interacted recently."""
        now = time.time()
        for uid in list(self._message_timestamps.keys()):
            q = self._message_timestamps[uid]
            self._cleanup_old_timestamps(q, SUSTAINED_WINDOW_SECONDS, now)
            if not q and self._cooldowns.get(uid, 0.0) < now:
                self._message_timestamps.pop(uid, None)
                self._callback_timestamps.pop(uid, None)
                self._cooldowns.pop(uid, None)
                self._warned_cooldowns.discard(uid)


class UserConcurrencyManager:
    """
    Prevents a user from running multiple heavy concurrent ERP or LMS scraping tasks simultaneously.
    Eliminates race conditions and session corruption.
    """

    def __init__(self):
        self._active_users: set[int] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def task_guard(self, user_id: Optional[int]):
        """
        Async context manager.
        Yields True if lock acquired, False if the user already has a running task.
        """
        if user_id is None:
            yield True
            return

        async with self._lock:
            if user_id in self._active_users:
                yield False
                return
            self._active_users.add(user_id)

        try:
            yield True
        finally:
            async with self._lock:
                self._active_users.discard(user_id)

    def is_user_busy(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        return user_id in self._active_users


class BackendThrottle:
    """
    Global gates for college ERP and LMS endpoints.
    Delegates to workload_manager.erp_gate and workload_manager.lms_gate
    which combine concurrency semaphores with token/leaky-bucket rate limiters.
    """
    @property
    def erp_semaphore(self):
        from workload import workload_manager
        return workload_manager.erp_gate

    @property
    def lms_semaphore(self):
        from workload import workload_manager
        return workload_manager.lms_gate


class BruteForceTracker:
    """
    Tracks consecutive failed ERP login attempts to prevent university account lockouts.
    """

    def __init__(self):
        self._failed_attempts: dict[int, list[float]] = defaultdict(list)
        self._lockouts: dict[int, float] = {}

    def is_locked(self, user_id: int) -> Tuple[bool, int]:
        now = time.time()
        lock_end = self._lockouts.get(user_id, 0.0)
        if now < lock_end:
            return True, int(lock_end - now)
        if user_id in self._lockouts:
            self._lockouts.pop(user_id, None)
            self._failed_attempts.pop(user_id, None)
        return False, 0

    def record_failure(self, user_id: int) -> bool:
        """Record a login failure. Returns True if lockout was just triggered."""
        now = time.time()
        cutoff = now - 600.0  # 10 minute window
        attempts = [t for t in self._failed_attempts[user_id] if t >= cutoff]
        attempts.append(now)
        self._failed_attempts[user_id] = attempts

        if len(attempts) >= MAX_FAILED_LOGINS:
            self._lockouts[user_id] = now + LOGIN_LOCKOUT_SECONDS
            logger.warning(
                "User %s locked out of login for %ds after %d consecutive failed attempts.",
                user_id,
                LOGIN_LOCKOUT_SECONDS,
                len(attempts),
            )
            return True
        return False

    def record_success(self, user_id: int) -> None:
        self._failed_attempts.pop(user_id, None)
        self._lockouts.pop(user_id, None)


# --- Global Singleton Instances ---
rate_limiter = RateLimiter()
user_concurrency = UserConcurrencyManager()
backend_throttle = BackendThrottle()
brute_force_tracker = BruteForceTracker()


def validate_input_length(text: Optional[str], max_length: int = MAX_INPUT_LENGTH) -> bool:
    """Verify input string does not exceed safe buffer thresholds."""
    if not text:
        return True
    return len(text.strip()) <= max_length


async def anti_flood_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Global pre-routing middleware (registered at group=-1).
    Intercepts and halts all flood attacks before any command or callback logic executes.
    """
    user = update.effective_user
    if not user:
        return

    is_allowed, is_first_warn, remaining = rate_limiter.check_rate_limit(update)
    if is_allowed:
        return

    # User violated rate limit or is in cooldown
    if is_first_warn:
        warning_msg = (
            "⚠️ <b>Anti-Flood Protection</b>\n\n"
            f"You are sending requests too fast. Please slow down!\n"
            f"Temporary cooldown active for <b>{int(remaining)} seconds</b>."
        )
        try:
            if update.callback_query:
                await update.callback_query.answer(
                    f"⚠️ Too fast! Please wait {int(remaining)}s.",
                    show_alert=True,
                )
            elif update.effective_message:
                await update.effective_message.reply_text(
                    warning_msg,
                    parse_mode="HTML",
                )
        except Exception:
            pass

    # Stop all further processing for this update
    raise ApplicationHandlerStop
