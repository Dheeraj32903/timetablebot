import asyncio
import html as html_lib
import logging
import random
import re
import time
from datetime import datetime, time as dt_time

from pathlib import Path
from typing import Optional, Tuple

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    JobQueue,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import credentials
import security
import storage
from erp.session import perform_login, fetch_timetable_html, load_session, warm_active_sessions
from erp.client import auto_login, reset_user_client
from parser.timetable import (
    format_timetable_for_telegram,
    get_timetable_messages,
    _today_day_name,
    _normalize_day_name,
    resolve_day_name,
)
from lms.client import LMSClient
from lms.service import LMSService
from outpass_ui import (
    outpass_command,
    handle_outpass_callback,
    handle_outpass_text_reason,
)
from workload import workload_manager, Job, JobMetrics

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states for collecting credentials in Telegram chat.
STATE_WAITING_USERNAME = "waiting_username"
STATE_WAITING_PASSWORD = "waiting_password"
STATE_WAITING_LMS_USERNAME = "waiting_lms_username"
STATE_WAITING_LMS_PASSWORD = "waiting_lms_password"

BOT_VERSION = "5.3.1"

# Daily automatic timetable message job names.
MORNING_TIMETABLE_JOB_NAME = "morning_timetable"
MORNING_PREFETCH_JOB_NAME = "morning_prefetch"
LMS_ALERTS_JOB_NAME = "lms_alerts_checker"
_morning_cache: dict[int, str] = {}




def _is_morning_enabled(user_id: Optional[int] = None) -> bool:
    """Return True if daily morning timetable is enabled for this user or any user."""
    if user_id is not None:
        return storage.is_user_morning_enabled(user_id)
    return bool(storage.get_all_morning_users())


def _set_morning_enabled(enabled: bool, user_id: Optional[int] = None) -> None:
    """Enable or disable daily morning timetable for a user."""
    if user_id is not None:
        storage.set_user_morning_enabled(user_id, enabled)


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not config.AUTHORIZED_TELEGRAM_USER_IDS:
        return True
    return user.id in config.AUTHORIZED_TELEGRAM_USER_IDS


BOT_COMMANDS = [
    BotCommand("menu", "Main dashboard & quick action menu"),
    BotCommand("timetable", "Today's classes, timings & rooms"),
    BotCommand("week", "Full weekly timetable view"),
    BotCommand("submissions", "Pending assignments & LMS deadlines"),
    BotCommand("outpass", "Hostel outpass records & live status"),
    BotCommand("friend", "Lookup classmate's timetable"),
    BotCommand("login", "Connect & save ERP credentials"),
    BotCommand("lms_login", "Connect & save LMS credentials"),
    BotCommand("lms_logout", "Disconnect LMS credentials"),
    BotCommand("morning", "Toggle daily morning schedule alert"),
    BotCommand("status", "Check ERP & LMS account status"),
    BotCommand("description", "Why use this bot & key benefits"),
    BotCommand("privacy", "View bot privacy policy"),
    BotCommand("logout", "Log out ERP & clear your data"),
]

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def get_timetable_keyboard(current_day: str) -> Optional[InlineKeyboardMarkup]:
    """Return None to keep timetable responses clean without inline buttons."""
    return None


def _clean_display_name(raw_name: str) -> str:
    """Format Telegram first_name cleanly, stripping leading student ID or symbols."""
    if not raw_name:
        return "Student"
    cleaned = re.sub(r"^\d+[\s_\-]+", "", raw_name.strip())
    cleaned = cleaned.strip(" _-")
    return cleaned or raw_name.strip()


def _build_dashboard_payload(user_id: int, first_name: str = "Student") -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Generate a clean, simple student hub dashboard card without inline buttons."""
    saved_erp = credentials.load_user_credentials(user_id)
    saved_lms = credentials.load_user_lms_credentials(user_id)
    erp_session = load_session(user_id=user_id)
    morning_on = storage.is_user_morning_enabled(user_id)

    clean_name = _clean_display_name(first_name)
    name_escaped = html_lib.escape(clean_name or "Student")

    student_id = (saved_erp or {}).get("username") or (saved_lms or {}).get("username") or ""
    if not student_id and first_name:
        m = re.search(r"^\d+", first_name.strip())
        if m:
            student_id = m.group(0)

    id_line = f"<b>Student ID:</b> <code>{html_lib.escape(student_id)}</code>\n" if student_id else ""

    # ERP status badge
    if saved_erp and erp_session and erp_session.get("cookies"):
        erp_badge = "🟢 <b>Active</b>"
    elif saved_erp:
        erp_badge = "🟡 <b>Stored</b>"
    else:
        erp_badge = "🔴 <b>Not Connected</b> (/login)"

    # LMS status badge
    if saved_lms:
        lms_badge = "🟢 <b>Connected</b>"
    else:
        lms_badge = "⚪ <b>Not Connected</b> (/lms_login)"

    # Morning alert badge
    if morning_on:
        morning_badge = f"🔔 <b>Enabled</b> (Daily {config.MORNING_TIMETABLE_HOUR:02d}:{config.MORNING_TIMETABLE_MINUTE:02d} IST)"
    else:
        morning_badge = "🔕 <b>Disabled</b>"

    text = (
        f"<b>Welcome, {name_escaped}</b>\n"
        f"<i>KL University Student Hub</i>\n\n"
        f"{id_line}"
        f"• <b>ERP Schedule:</b> {erp_badge}\n"
        f"• <b>Moodle LMS:</b> {lms_badge}\n"
        f"• <b>Morning Dispatch:</b> {morning_badge}\n\n"
        f"<i>Use the <b>Menu</b> button below to access all commands and services.</i>"
    )

    return text, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid = user.id if user else None

    if not is_authorized(update):
        if uid:
            try:
                await context.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=uid))
            except Exception:
                pass
        if update.message:
            await update.message.reply_text(
                "⛔ <b>Access Denied.</b>\nYou are not authorized to use this bot.",
                parse_mode="HTML",
            )
        return

    # Ensure native Telegram command menu is active for authorized user
    if uid:
        try:
            await context.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=uid))
        except Exception:
            pass

    text, markup = _build_dashboard_payload(uid, user.first_name if user else "Student")
    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=markup,
        )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the interactive student hub dashboard."""
    if not is_authorized(update):
        await update.effective_message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    text, markup = _build_dashboard_payload(uid, user.first_name if user else "Student")
    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=markup,
    )


async def _run_login(update: Update, username: str, password: str) -> None:
    """Run the async ERP login and report the result."""
    user = update.effective_user
    uid = user.id if user else None

    if uid:
        is_locked, remaining = security.brute_force_tracker.is_locked(uid)
        if is_locked:
            mins, secs = divmod(remaining, 60)
            await update.message.reply_text(
                f"🔒 <b>Login Temporarily Locked</b>\n\n"
                f"Too many failed login attempts detected. To prevent university ERP account lockouts, "
                f"please wait <b>{mins}m {secs}s</b> before trying again.",
                parse_mode="HTML",
            )
            return

    async with security.user_concurrency.task_guard(uid) as acquired:
        if not acquired:
            await update.message.reply_text(
                "⏳ <b>Request in progress.</b>\nYour previous request is still running. Please wait for it to finish.",
                parse_mode="HTML",
            )
            return

        await update.message.reply_text("⏳ Logging in to ERP...")
        try:
            await perform_login(username, password, user_id=uid)

            # Save credentials for this Telegram user for future automatic use.
            if uid:
                security.brute_force_tracker.record_success(uid)
                credentials.save_user_credentials(uid, username, password)

            await update.message.reply_text(
                "✅ <b>Logged in successfully.</b>\n"
                "Send /timetable to get your timetable.",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.exception("Login failed")
            if uid:
                locked = security.brute_force_tracker.record_failure(uid)
                if locked:
                    await update.message.reply_text(
                        "🔒 <b>Security Alert:</b> Too many failed login attempts.\n"
                        "Login has been temporarily locked for 15 minutes to protect your student ERP account from being blocked by the university.",
                        parse_mode="HTML",
                    )
                    return
            await update.message.reply_text(
                "❌ <b>Login failed.</b> Please check your credentials and try again.",
                parse_mode="HTML",
            )


async def _run_friend_login(update: Update, username: str, password: str) -> None:
    """Run ephemeral login to fetch a friend's timetable without touching stored user sessions."""
    user = update.effective_user
    uid = user.id if user else 0
    msg = await update.message.reply_text("⏳ <i>Logging in and fetching friend's timetable...</i>", parse_mode="HTML")
    try:
        async def _friend_fetch():
            login_res = await auto_login(username, password)
            cookies = login_res["cookies"]
            csrf = login_res.get("csrf_token", "")
            return await fetch_timetable_html(cookies=cookies, csrf_token=csrf)

        html = await workload_manager.submit_job(
            workload_manager.timetable_queue,
            uid,
            "fetch_friend_timetable",
            "timetable",
            _friend_fetch,
        )

        today_name = _today_day_name()
        messages = get_timetable_messages(html, day_filter=today_name)
        if not messages:
            await msg.edit_text("⚠️ <i>Could not extract friend's timetable.</i>", parse_mode="HTML")
            return

        u_escaped = html_lib.escape(username)
        await msg.edit_text(
            f"👥 <b>Timetable for {u_escaped} ({today_name}):</b>\n\n{messages[0]}",
            parse_mode="HTML",
        )
        for extra in messages[1:]:
            await update.message.reply_text(extra, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Friend login or fetch failed: %s", exc)
        await msg.edit_text(
            f"❌ <b>Could not fetch friend's timetable.</b>\n"
            f"<i>{html_lib.escape(security.sanitize_user_error(exc, 'Invalid credentials or ERP temporarily unavailable.'))}</i>\n\n"
            "Please check the username and password and try again.",
            parse_mode="HTML",
        )


async def friend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lookup a classmate's timetable using ephemeral in-memory login."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    context.user_data["friend_login"] = True
    context.user_data["login_state"] = STATE_WAITING_USERNAME
    await update.message.reply_text(
        "👥 <b>Friend's Timetable (Step 1 of 2)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Please send your friend's ERP <b>username / Student ID</b>:\n\n"
        "<i>(Their credentials will be used once in-memory and will never be saved to database)</i>\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML",
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    # Check brute-force lockout status
    is_locked, remaining = security.brute_force_tracker.is_locked(user.id)
    if is_locked:
        mins, secs = divmod(remaining, 60)
        await update.message.reply_text(
            f"🔒 <b>Login Temporarily Locked</b>\n\n"
            f"Too many failed login attempts detected. To prevent university ERP account lockouts, "
            f"please wait <b>{mins}m {secs}s</b> before trying again.",
            parse_mode="HTML",
        )
        return

    # Use stored credentials if available.
    saved = credentials.load_user_credentials(user.id)
    if saved:
        await _run_login(update, saved["username"], saved["password"])
        return

    # If credentials are configured in .env, use them directly.
    if config.ERP_USERNAME and config.ERP_PASSWORD:
        await _run_login(update, config.ERP_USERNAME, config.ERP_PASSWORD)
        return

    # Otherwise, start a conversation to collect credentials in Telegram chat.
    context.user_data["login_state"] = STATE_WAITING_USERNAME
    await update.message.reply_text("Send your ERP username / Student ID.")


async def handle_login_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle username/password replies during the /login, /friend, or /lms conversation."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Check if user is typing a custom outpass reason
    if await handle_outpass_text_reason(update, context):
        return

    text = update.message.text.strip()

    # Input length validation guard against buffer overflow / memory exhaustion
    if not security.validate_input_length(text, max_length=security.MAX_INPUT_LENGTH):
        await update.message.reply_text(
            f"❌ <b>Input too long.</b> Maximum allowed length is {security.MAX_INPUT_LENGTH} characters.",
            parse_mode="HTML",
        )
        return

    state = context.user_data.get("login_state")
    text = update.message.text.strip()

    if text.lower() in {"cancel", "/cancel"}:
        context.user_data.pop("login_state", None)
        context.user_data.pop("erp_username", None)
        context.user_data.pop("lms_username", None)
        context.user_data.pop("friend_login", None)
        context.user_data.pop("outpass_flow", None)
        await update.message.reply_text("❌ Action cancelled.")
        return

    # --- ERP Login Flow ---
    if state == STATE_WAITING_USERNAME:
        context.user_data["erp_username"] = text
        context.user_data["login_state"] = STATE_WAITING_PASSWORD
        await update.message.reply_text("Send your ERP password.\n<i>(Your message will be automatically deleted)</i>", parse_mode="HTML")
        return

    if state == STATE_WAITING_PASSWORD:
        username = context.user_data.pop("erp_username", "")
        is_friend = context.user_data.pop("friend_login", False)
        context.user_data.pop("login_state", None)
        password = text

        # Immediately delete the password message from Telegram chat for user privacy
        try:
            await update.message.delete()
        except Exception:
            pass

        if not username:
            await update.message.reply_text("⚠️ Something went wrong. Start again.")
            return

        if is_friend:
            await _run_friend_login(update, username, password)
        else:
            await _run_login(update, username, password)
        return

    # --- LMS Login Flow ---
    if state == STATE_WAITING_LMS_USERNAME:
        context.user_data["lms_username"] = text
        context.user_data["login_state"] = STATE_WAITING_LMS_PASSWORD
        await update.message.reply_text(
            "<b>🔗Connect to LMS</b>\n\n"
            "Please enter your password:\n"
            "<i>(Your message will be automatically deleted)</i>",
            parse_mode="HTML",
        )
        return

    if state == STATE_WAITING_LMS_PASSWORD:
        username = context.user_data.pop("lms_username", "")
        context.user_data.pop("login_state", None)
        password = text

        try:
            await update.message.delete()
        except Exception:
            pass

        if not username:
            await update.message.reply_text("⚠️ Something went wrong. Please run /lms_login to try again.")
            return

        await _run_lms_login(update, context, username, password)
        return

    # --- Outpass Custom Reason Input ---
    if await handle_outpass_text_reason(update, context):
        return

    # If we are not in a login conversation, ignore plain text.
    await update.message.reply_text(
        "💡 Use /timetable for today's classes, /submissions for pending assignments &amp; deadlines, or /login to connect ERP.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# LMS Handlers & Deadline Alerts
# ---------------------------------------------------------------------------

async def _run_lms_login(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str, password: str) -> None:
    """Authenticate with KL Moodle LMS, encrypt credentials in MongoDB, and fetch pending tasks."""
    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    status_msg = await update.message.reply_text("⏳ <b>Connecting to KL LMS...</b>", parse_mode="HTML")
    try:
        client = LMSClient()
        token = await client.get_moodle_token(username, password)

        # Save credentials with 256-bit Fernet encryption in MongoDB
        credentials.save_user_lms_credentials(uid, username, password)
        credentials.save_user_lms_token(uid, token)

        try:
            site_info = await client.get_site_info()
            fullname = site_info.get("fullname", username)
        except Exception:
            fullname = username

        await status_msg.edit_text(
            f"✅ <b>LMS Connected Successfully!</b>\n"
            f"Welcome, <b>{fullname}</b> (ID: <code>{username}</code>).\n\n"
            f"Fetching your pending tasks and deadlines...",
            parse_mode="HTML",
        )
        await _send_lms_pending_tasks(update, context, uid)
    except ValueError as exc:
        await status_msg.edit_text(
            f"❌ <b>LMS Login Failed</b>\n{str(exc)}\n\nPlease try /lms_login again with your correct Student ID and password.",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("LMS login failed: %s", exc)
        await status_msg.edit_text(
            "❌ <b>Could not connect to KL LMS.</b>\nPlease verify your connection and try again later.",
            parse_mode="HTML",
        )


async def _send_lms_pending_tasks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    message_to_edit=None,
    force_refresh: bool = False,
) -> None:
    """Fetch pending tasks from LMS and display the interactive card (with multi-part chunking if tasks exceed limit)."""
    try:
        tasks = await LMSService.fetch_all_pending_tasks(user_id, force_refresh=force_refresh)
        _, student_name = await LMSService.get_authenticated_client(user_id)
        messages = LMSService.get_tasks_telegram_messages(tasks, student_name=student_name)

        # Track seen tasks to prevent duplicate alerts
        state = storage.get_lms_task_state(user_id)
        current_seen = set(state.get("seen_task_ids", []))
        for t in tasks:
            current_seen.add(t.id)
        storage.update_lms_task_state(
            user_id,
            list(current_seen),
            state.get("reminded_48h", []),
            state.get("reminded_6h", []),
        )

        first_text = messages[0] if messages else "<b>All Caught Up</b>"

        if message_to_edit:
            await message_to_edit.edit_text(
                first_text,
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )
        else:
            await update.effective_message.reply_text(
                first_text,
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )

        # Dispatch any subsequent overflow chunks to prevent 4096 character errors
        for extra_text in messages[1:]:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=extra_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as send_err:
                logger.warning("Could not send extra task chunk: %s", send_err)
    except Exception as exc:
        logger.exception("Error fetching pending tasks for user %s: %s", user_id, exc)
        err_msg = (
            "⚠️ <b>Could not fetch LMS tasks.</b>\n"
            f"<i>{html_lib.escape(security.sanitize_user_error(exc, 'Could not connect to LMS service.'))}</i>\n\n"
            "If your LMS password changed, please run /lms_login to reconnect."
        )
        if message_to_edit:
            await message_to_edit.edit_text(err_msg, parse_mode="HTML")
        else:
            await update.effective_message.reply_text(err_msg, parse_mode="HTML")


async def submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View pending HAs, ALMs, quizzes, and deadlines from KL LMS."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    async with security.user_concurrency.task_guard(user.id) as acquired:
        if not acquired:
            await update.message.reply_text(
                "⏳ <b>Request in progress.</b>\nYour previous request is still running. Please wait for it to finish.",
                parse_mode="HTML",
            )
            return

        saved_lms = credentials.load_user_lms_credentials(user.id)
        if not saved_lms:
            # Submissions are strictly visible only if the user is logged into LMS
            await update.message.reply_text(
                "<b>LMS Not Connected</b>\n\n"
                "Please connect your KL LMS account first using /lms_login to view your pending submissions and deadlines.",
                parse_mode="HTML",
            )
            return

        # Instant UX feedback within 50ms
        if update.effective_chat:
            try:
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
            except Exception:
                pass

        status_msg = await update.message.reply_text("⚡ <i>Fetching live LMS submissions...</i>", parse_mode="HTML")
        await workload_manager.submit_coalesced_job(
            (user.id, "lms_tasks"),
            workload_manager.lms_queue,
            user.id,
            "fetch_lms_tasks",
            "lms",
            _send_lms_pending_tasks,
            update,
            context,
            user.id,
            message_to_edit=status_msg,
            force_refresh=True,
        )


# Keep /lms as an alias to /submissions
lms = submissions


async def lms_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Connect or update stored LMS credentials."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    context.user_data["login_state"] = STATE_WAITING_LMS_USERNAME
    await update.message.reply_text(
        "<b>Connect to LMS</b>\n\n"
        "Please enter your username:",
        parse_mode="HTML",
    )


async def lms_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disconnect and purge stored LMS credentials."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    credentials.delete_user_lms_credentials(user.id)
    LMSService.invalidate_user_cache(user.id)
    await update.message.reply_text(
        "✅ <b>LMS Disconnected.</b>\nYour stored LMS credentials, tokens, and alert history have been completely removed.",
        parse_mode="HTML",
    )


async def handle_lms_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks on the LMS card view."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = update.effective_user
    if not user or not is_authorized(update):
        return

    if data == "lms:refresh":
        await query.edit_message_text("🔄 <i>Refreshing pending tasks from LMS...</i>", parse_mode="HTML")
        await workload_manager.submit_job(
            workload_manager.lms_queue,
            user.id,
            "refresh_lms_tasks",
            "lms",
            _send_lms_pending_tasks,
            update,
            context,
            user.id,
            message_to_edit=query.message,
            force_refresh=True,
        )
    elif data == "lms:logout":
        credentials.delete_user_lms_credentials(user.id)
        LMSService.invalidate_user_cache(user.id)
        await query.edit_message_text(
            "✅ <b>LMS Disconnected.</b>\nYour LMS credentials have been removed. Use /lms_login to reconnect anytime.",
            parse_mode="HTML",
        )
    elif data == "lms:cancel_login":
        context.user_data.pop("login_state", None)
        context.user_data.pop("lms_username", None)
        await query.edit_message_text("❌ <b>LMS connection cancelled.</b>", parse_mode="HTML")


async def check_lms_deadlines_and_new_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Periodic background job:
    1. Detects newly assigned tasks in any subject.
    2. Sends 2-Day Deadline reminder (<= 48h remaining).
    3. Sends Urgent Same-Day alert (<= 6h remaining).
    """
    user_ids = storage.get_all_lms_users()
    if not user_ids:
        return

    logger.info("Running periodic LMS deadline & new task scan for %d users...", len(user_ids))

    for uid in user_ids:
        user_lock = await workload_manager.user_locks.get_lock(uid)
        async with user_lock:
            try:
                tasks = await LMSService.fetch_all_pending_tasks(uid)
                state = storage.get_lms_task_state(uid)
                seen_ids = set(state.get("seen_task_ids", []))
                reminded_48h = set(state.get("reminded_48h", []))
                reminded_6h = set(state.get("reminded_6h", []))

                is_first_sync = len(seen_ids) == 0

                # 1. Detect New Tasks
                for task in tasks:
                    if not is_first_sync and task.id not in seen_ids and task.seconds_remaining > 0:
                        alert_text = LMSService.format_new_task_alert(task)
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=alert_text,
                                parse_mode="HTML",
                                reply_markup=None,
                                disable_web_page_preview=True,
                            )
                            logger.info("Sent new LMS task alert (%s) to user %s", task.id, uid)
                            await asyncio.sleep(1.5)
                        except Exception as exc:
                            logger.warning("Could not send new task alert to user %s: %s", uid, exc)

                    seen_ids.add(task.id)

                # 2. Check 2-Day (48h) Deadlines
                for task in tasks:
                    rem_secs = task.seconds_remaining
                    if 6 * 3600 < rem_secs <= 48 * 3600:
                        if task.id not in reminded_48h:
                            alert_text = LMSService.format_2day_reminder_alert(task)
                            try:
                                await context.bot.send_message(
                                    chat_id=uid,
                                    text=alert_text,
                                    parse_mode="HTML",
                                    reply_markup=None,
                                    disable_web_page_preview=True,
                                )
                                reminded_48h.add(task.id)
                                logger.info("Sent 2-day reminder for task %s to user %s", task.id, uid)
                                await asyncio.sleep(1.5)
                            except Exception as exc:
                                logger.warning("Could not send 2-day reminder to user %s: %s", uid, exc)

                # 3. Check Urgent (<6h) Deadlines
                for task in tasks:
                    rem_secs = task.seconds_remaining
                    if 0 < rem_secs <= 6 * 3600:
                        if task.id not in reminded_6h:
                            alert_text = LMSService.format_urgent_reminder_alert(task)
                            try:
                                await context.bot.send_message(
                                    chat_id=uid,
                                    text=alert_text,
                                    parse_mode="HTML",
                                    reply_markup=None,
                                    disable_web_page_preview=True,
                                )
                                reminded_6h.add(task.id)
                                logger.info("Sent urgent reminder for task %s to user %s", task.id, uid)
                                await asyncio.sleep(1.5)
                            except Exception as exc:
                                logger.warning("Could not send urgent alert to user %s: %s", uid, exc)

                # Save state
                storage.update_lms_task_state(
                    uid,
                    list(seen_ids),
                    list(reminded_48h),
                    list(reminded_6h),
                )

            except Exception as exc:
                logger.error("Error during LMS scan for user %s: %s", uid, exc)

        await asyncio.sleep(2.0)


def _schedule_lms_alerts_job(application: Application) -> None:
    """Schedule the periodic LMS deadline & new task alert job (every 60 minutes)."""
    job_queue = application.job_queue
    if not job_queue:
        return

    if not job_queue.get_jobs_by_name(LMS_ALERTS_JOB_NAME):
        job_queue.run_repeating(
            check_lms_deadlines_and_new_tasks,
            interval=3600,
            first=60,
            name=LMS_ALERTS_JOB_NAME,
        )
        logger.info("Scheduled periodic LMS deadline & new task alert job (every 60 mins)")


async def _fetch_live_timetable_job(uid: int, metric=None) -> str:
    """Worker job handler to fetch live timetable from ERP (silent re-auth if needed)."""
    return await fetch_timetable_html(user_id=uid, metric=metric)


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Parse optional day argument, e.g. /timetable tomorrow or /timetable mon.
    arg = " ".join(context.args).strip() if context.args else ""
    day = resolve_day_name(arg)
    if day is None:
        await update.message.reply_text(
            "⚠️ Invalid day. Use: today, yesterday, tomorrow, mon, tue, wed, thu, fri, sat, sun."
        )
        return

    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    # Instant UX feedback within 50ms
    if update.effective_chat:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

    status_msg = await update.message.reply_text("⚡ <i>Fetching live data from ERP...</i>", parse_mode="HTML")

    try:
        html = await workload_manager.submit_coalesced_job(
            (uid, "timetable"),
            workload_manager.timetable_queue,
            uid,
            "fetch_timetable",
            "timetable",
            _fetch_live_timetable_job,
            uid,
        )

        context.user_data["last_timetable_html"] = html
        context.user_data["last_timetable_time"] = time.time()

        messages = get_timetable_messages(html, day_filter=day)
        if not messages:
            await status_msg.edit_text("⚠️ <i>Could not extract timetable. Please check in ERP.</i>", parse_mode="HTML")
            return

        kb = get_timetable_keyboard(day)
        await status_msg.edit_text(
            messages[0],
            parse_mode="HTML",
            reply_markup=kb,
        )
        for extra_msg in messages[1:]:
            await update.message.reply_text(extra_msg, parse_mode="HTML")

    except Exception as exc:
        err_msg = str(exc)
        if "No active ERP session" in err_msg:
            await status_msg.edit_text(
                "⚠️ <b>No active ERP session.</b>\nPlease log in using /login first to fetch your live timetable.",
                parse_mode="HTML",
            )
        elif "Server is busy" in err_msg:
            await status_msg.edit_text(
                "⏳ <b>Server is busy.</b>\nToo many requests in queue. Please try again in a few moments.",
                parse_mode="HTML",
            )
        else:
            logger.exception("Failed to fetch timetable")
            await status_msg.edit_text(
                "⚠️ <b>Unable to fetch live timetable from ERP.</b>\n"
                "Please check directly in the ERP portal or try again later.",
                parse_mode="HTML",
            )


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    # Instant UX feedback within 50ms
    if update.effective_chat:
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

    status_msg = await update.message.reply_text("⚡ <i>Fetching live week timetable from ERP...</i>", parse_mode="HTML")

    try:
        html = await workload_manager.submit_coalesced_job(
            (uid, "week_timetable"),
            workload_manager.timetable_queue,
            uid,
            "fetch_week_timetable",
            "timetable",
            _fetch_live_timetable_job,
            uid,
        )

        context.user_data["last_timetable_html"] = html
        context.user_data["last_timetable_time"] = time.time()

        messages = get_timetable_messages(html)
        if not messages:
            await status_msg.edit_text("⚠️ <i>Could not extract timetable. Please check in ERP.</i>", parse_mode="HTML")
            return

        await status_msg.edit_text(
            messages[0],
            parse_mode="HTML",
            reply_markup=None,
        )
        for extra_msg in messages[1:]:
            await update.message.reply_text(extra_msg, parse_mode="HTML")

    except Exception as exc:
        err_msg = str(exc)
        if "No active ERP session" in err_msg:
            await status_msg.edit_text(
                "⚠️ <b>No active ERP session.</b>\nPlease log in using /login first to fetch your live timetable.",
                parse_mode="HTML",
            )
        elif "Server is busy" in err_msg:
            await status_msg.edit_text(
                "⏳ <b>Server is busy.</b>\nToo many requests in queue. Please try again in a few moments.",
                parse_mode="HTML",
            )
        else:
            logger.exception("Failed to fetch week timetable")
            await status_msg.edit_text(
                "⚠️ <b>Unable to fetch live timetable from ERP.</b>\n"
                "Please check directly in the ERP portal or try again later.",
                parse_mode="HTML",
            )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    if uid:
        try:
            reset_user_client(uid)
            storage.delete_user_all_data(uid)
            context.user_data.clear()
            if not storage.get_all_morning_users() and context.job_queue:
                for job in context.job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME):
                    job.schedule_removal()
                for job in context.job_queue.get_jobs_by_name(MORNING_PREFETCH_JOB_NAME):
                    job.schedule_removal()
        except Exception as exc:
            logger.error("Error during logout for %s: %s", uid, exc)

    await update.message.reply_text(
        "🚪 <b>Logged out successfully.</b>\n"
        "Use /login to log in again.",
        parse_mode="HTML",
    )


async def _prefetch_user_job(uid: int, saved_creds: dict, metric=None) -> Optional[str]:
    """Prefetch job for a single morning user: fetches live from ERP, saves to RAM and MongoDB."""
    try:
        try:
            html = await fetch_timetable_html(user_id=uid)
        except Exception:
            await perform_login(saved_creds["username"], saved_creds["password"], user_id=uid, max_retries=3)
            html = await fetch_timetable_html(user_id=uid)

        if html:
            _morning_cache[uid] = html
            storage.save_morning_cache(uid, html)
            logger.info("Morning prefetch successful for user %s", uid)
            return html
    except Exception as exc:
        logger.warning("Morning prefetch failed for user %s: %s", uid, exc)
    return None


async def _prefetch_morning_timetables(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bounded concurrent morning prefetch (starts at 06:30 IST): pre-authenticates and caches timetables in MongoDB."""
    today_name = _today_day_name()
    if today_name == "Sun":
        logger.info("Morning prefetch skipped: today is Sunday.")
        return

    morning_uids = storage.get_all_morning_users()
    if not morning_uids:
        return

    logger.info("Starting bounded concurrent morning pre-fetch for %d users...", len(morning_uids))
    _morning_cache.clear()

    for chat_id in morning_uids:
        saved = credentials.load_user_credentials(chat_id)
        if not saved:
            continue

        try:
            metric = JobMetrics(
                job_id=workload_manager._next_job_id("prefetch"),
                user_id=chat_id,
                operation="morning_prefetch",
                queue_name="prefetch",
                created_at=time.time(),
                enqueued_at=time.perf_counter(),
            )
            job = Job(
                metrics=metric,
                handler=_prefetch_user_job,
                args=(chat_id, saved),
            )
            await workload_manager.prefetch_queue.put(job)
        except Exception as exc:
            logger.error("Failed to enqueue prefetch job for user %s: %s", chat_id, exc)


async def _send_morning_timetable(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Morning delivery job (starts at 06:45 IST):
    Retrieves timetable using 3-tier cache hierarchy:
      Tier 1: RAM (_morning_cache)
      Tier 2: MongoDB (morning_cache collection)
      Tier 3: ERP Fallback (live fetch if prefetch expired)
    Dispatches via TelegramOutbox (rate-limited, honors 429).
    """
    today_name = _today_day_name()
    if today_name == "Sun":
        logger.info("Morning timetable delivery skipped: today is Sunday.")
        return

    morning_uids = storage.get_all_morning_users()
    if not morning_uids:
        logger.info("Morning timetable skipped: no users with morning alerts enabled.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    for chat_id in morning_uids:
        if storage.has_morning_alert_sent_today(chat_id, today_str):
            logger.info("Morning alert already sent today (%s) to %s, skipping.", today_str, chat_id)
            continue

        # Tier 1: In-memory RAM cache
        html = _morning_cache.get(chat_id)

        # Tier 2: MongoDB persistent morning cache
        if not html:
            html = storage.load_morning_cache(chat_id)
            if html:
                _morning_cache[chat_id] = html

        # Tier 3: Live ERP fallback
        if not html:
            saved = credentials.load_user_credentials(chat_id)
            if saved:
                try:
                    try:
                        html = await fetch_timetable_html(user_id=chat_id)
                    except Exception:
                        await perform_login(saved["username"], saved["password"], user_id=chat_id, max_retries=2)
                        html = await fetch_timetable_html(user_id=chat_id)
                except Exception as exc:
                    logger.warning("Tier 3 ERP fallback failed for user %s: %s", chat_id, exc)

        if html:
            try:
                messages = get_timetable_messages(html, day_filter=today_name)
                for idx, chunk in enumerate(messages):
                    header = f"🌅 <b>Good morning! Here is today's schedule ({today_name}):</b>\n\n" if idx == 0 else f"🌅 <b>Schedule ({today_name}) - Part {idx + 1}:</b>\n\n"
                    await workload_manager.outbox.send_message(
                        chat_id=chat_id,
                        text=f"{header}{chunk}",
                        parse_mode="HTML",
                    )
                storage.record_morning_alert_sent(chat_id, today_str)
                logger.info("Dispatched morning timetable to outbox for chat_id=%s (%d parts)", chat_id, len(messages))
            except Exception as e:
                logger.exception("Error formatting morning timetable for chat_id=%s: %s", chat_id, e)
        else:
            await workload_manager.outbox.send_message(
                chat_id=chat_id,
                text="⚠️ <b>Unable to fetch live timetable from ERP this morning.</b>\nPlease check directly in the ERP portal.",
                parse_mode="HTML",
            )
            storage.record_morning_alert_sent(chat_id, today_str)

    # Clear memory cache after delivery
    _morning_cache.clear()


def _schedule_morning_timetable_job(context) -> None:
    """Schedule the daily morning timetable pre-fetch (06:30 IST) and delivery (06:45 IST) jobs."""
    if not _is_morning_enabled():
        return

    job_queue = getattr(context, "job_queue", None)
    if not job_queue:
        return

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = None

    # Delivery time (e.g., 06:45 IST)
    scheduled_time = dt_time(
        hour=config.MORNING_TIMETABLE_HOUR,
        minute=config.MORNING_TIMETABLE_MINUTE,
        tzinfo=tz,
    )

    # Pre-fetch warmup time (15 minutes earlier, e.g., 06:30 IST)
    prefetch_hour = config.MORNING_TIMETABLE_HOUR
    prefetch_minute = config.MORNING_TIMETABLE_MINUTE - 15
    if prefetch_minute < 0:
        prefetch_minute += 60
        prefetch_hour = (prefetch_hour - 1) % 24

    prefetch_time = dt_time(
        hour=prefetch_hour,
        minute=prefetch_minute,
        tzinfo=tz,
    )

    if not job_queue.get_jobs_by_name(MORNING_PREFETCH_JOB_NAME):
        job_queue.run_daily(
            _prefetch_morning_timetables,
            time=prefetch_time,
            name=MORNING_PREFETCH_JOB_NAME,
        )
        logger.info(
            "Scheduled morning pre-fetch warmup job at %02d:%02d IST",
            prefetch_hour,
            prefetch_minute,
        )

    if not job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME):
        job_queue.run_daily(
            _send_morning_timetable,
            time=scheduled_time,
            name=MORNING_TIMETABLE_JOB_NAME,
        )
        logger.info(
            "Scheduled morning timetable delivery job at %02d:%02d IST",
            config.MORNING_TIMETABLE_HOUR,
            config.MORNING_TIMETABLE_MINUTE,
        )



async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the daily morning timetable message."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return

    saved = credentials.load_user_credentials(user.id)
    if not saved and not config.ERP_USERNAME:
        await update.message.reply_text(
            "⚠️ No stored credentials found. Please log in with /login first."
        )
        return

    is_enabled = _is_morning_enabled(user.id)
    if is_enabled:
        _set_morning_enabled(False, user.id)
        if not _is_morning_enabled():
            if context.job_queue:
                for job in context.job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME):
                    job.schedule_removal()
                for job in context.job_queue.get_jobs_by_name(MORNING_PREFETCH_JOB_NAME):
                    job.schedule_removal()
        await update.message.reply_text(
            "🔕 <b>Daily morning alert disabled.</b>\n"
            "You will not receive automatic morning schedules.\n\n"
            "<i>To enable it again, send /morning</i>",
            parse_mode="HTML",
        )
    else:
        _set_morning_enabled(True, user.id)
        _schedule_morning_timetable_job(context)
        await update.message.reply_text(
            f"🔔 <b>Daily morning alert enabled!</b>\n"
            f"The bot will automatically send your day's schedule every morning at "
            f"<b>{config.MORNING_TIMETABLE_HOUR:02d}:{config.MORNING_TIMETABLE_MINUTE:02d} IST</b>.\n\n"
            f"<i>To disable it anytime, send /morning</i>",
            parse_mode="HTML",
        )


def _build_status_payload(user_id: int) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Generate a rich, interactive account and integration status dashboard."""
    saved_erp = credentials.load_user_credentials(user_id)
    saved_lms = credentials.load_user_lms_credentials(user_id)
    erp_session = load_session(user_id=user_id)
    morning_on = storage.is_user_morning_enabled(user_id)

    if saved_erp:
        has_cookies = bool(erp_session and erp_session.get("cookies"))
        cookie_stat = "Active Live Session" if has_cookies else "Session Expired (Auto re-login ready)"
        erp_status_line = f"🟢 <b>Connected</b> (<code>{html_lib.escape(saved_erp.get('username', ''))}</code>)\n   └ <i>{cookie_stat}</i>"
    else:
        erp_status_line = "🔴 <b>Disconnected</b> (Use /login to link account)"

    if saved_lms:
        lms_status_line = f"🟢 <b>Connected</b> (<code>{html_lib.escape(saved_lms.get('username', ''))}</code>)\n   └ <i>Moodle Token Active</i>"
    else:
        lms_status_line = "⚪ <b>Disconnected</b> (Use /lms_login to link account)"

    if morning_on:
        morning_status_line = f"🔔 <b>Enabled</b> (Daily {config.MORNING_TIMETABLE_HOUR:02d}:{config.MORNING_TIMETABLE_MINUTE:02d} IST)"
    else:
        morning_status_line = "🔕 <b>Disabled</b>"

    text = (
        "📊 <b>Account & Integration Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n\n"
        f"🏫 <b>University ERP:</b>\n{erp_status_line}\n\n"
        f"📚 <b>Moodle LMS:</b>\n{lms_status_line}\n\n"
        f"⏰ <b>Morning Dispatch:</b>\n{morning_status_line}\n\n"
        f"🔐 <b>Storage Encryption:</b>\n🛡️ Authenticated Fernet (AES-128-CBC + HMAC-SHA256)\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    return text, None


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    text, markup = _build_status_payload(uid)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle interactions from the main dashboard inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    uid = user.id if user else None
    if not uid or not is_authorized(update):
        return

    if data == "menu:open":
        text, markup = _build_dashboard_payload(uid, user.first_name if user else "Student")
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data.startswith("menu:timetable:"):
        target_day = data.split(":", 2)[2]
        await query.edit_message_text("⚡ <i>Fetching live data from ERP...</i>", parse_mode="HTML")
        try:
            html = await workload_manager.submit_coalesced_job(
                (uid, "timetable"),
                workload_manager.timetable_queue,
                uid,
                "fetch_timetable",
                "timetable",
                _fetch_live_timetable_job,
                uid,
            )
            context.user_data["last_timetable_html"] = html
            context.user_data["last_timetable_time"] = time.time()
            messages = get_timetable_messages(html, day_filter=target_day)
            kb = get_timetable_keyboard(target_day)
            await query.edit_message_text(messages[0] if messages else "⚠️ <i>Empty schedule</i>", parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            await query.edit_message_text(f"⚠️ <i>{html_lib.escape(security.sanitize_user_error(exc, 'Could not load timetable from ERP.'))}</i>", parse_mode="HTML")
        return

    if data == "menu:week":
        await query.edit_message_text("⚡ <i>Fetching live week timetable from ERP...</i>", parse_mode="HTML")
        try:
            html = await workload_manager.submit_coalesced_job(
                (uid, "week_timetable"),
                workload_manager.timetable_queue,
                uid,
                "fetch_week_timetable",
                "timetable",
                _fetch_live_timetable_job,
                uid,
            )
            context.user_data["last_timetable_html"] = html
            context.user_data["last_timetable_time"] = time.time()
            messages = get_timetable_messages(html)
            await query.edit_message_text(messages[0] if messages else "⚠️ <i>Empty schedule</i>", parse_mode="HTML", reply_markup=None)
            for extra in messages[1:]:
                await context.bot.send_message(chat_id=uid, text=extra, parse_mode="HTML")
        except Exception as exc:
            await query.edit_message_text(f"⚠️ <i>{html_lib.escape(security.sanitize_user_error(exc, 'Could not load week timetable from ERP.'))}</i>", parse_mode="HTML")
        return

    if data == "menu:submissions":
        saved_lms = credentials.load_user_lms_credentials(uid)
        if not saved_lms:
            await query.edit_message_text(
                "<b>LMS Not Connected</b>\n\n"
                "Please connect your KL Moodle LMS account first using /lms_login to view your pending submissions and deadlines.",
                parse_mode="HTML",
                reply_markup=None,
            )
            return

        await query.edit_message_text("⚡ <i>Fetching live submissions from LMS...</i>", parse_mode="HTML")
        await workload_manager.submit_coalesced_job(
            (uid, "lms_tasks"),
            workload_manager.lms_queue,
            uid,
            "fetch_lms_tasks",
            "lms",
            _send_lms_pending_tasks,
            update,
            context,
            uid,
            message_to_edit=query.message,
            force_refresh=True,
        )
        return

    if data == "menu:outpass":
        outpass_greeting = (
            "🏠 <b>Hostel Outpass Services</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Apply for digital hostel gate outing passes with instant warden routing.\n\n"
            "Send /outpass in chat to start the step-by-step application, or check your past requests."
        )
        await query.edit_message_text(outpass_greeting, parse_mode="HTML", reply_markup=None)
        return

    if data == "menu:friend":
        context.user_data["friend_login"] = True
        context.user_data["login_state"] = STATE_WAITING_USERNAME
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:open")]])
        await query.edit_message_text(
            "👥 <b>Friend's Timetable Lookup</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send your friend's ERP <b>username / Student ID</b>:\n\n"
            "<i>(Their credentials will be used once in-memory and will never be saved to database)</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb,
        )
        return

    if data == "menu:morning_toggle":
        is_on = storage.is_user_morning_enabled(uid)
        storage.set_user_morning_enabled(uid, not is_on)
        if not is_on:
            _schedule_morning_timetable_job(context)
            await query.answer("🔔 Daily morning alerts enabled at 06:45 AM IST!", show_alert=False)
        else:
            await query.answer("🔕 Daily morning alerts disabled.", show_alert=False)
        text, markup = _build_dashboard_payload(uid, user.first_name if user else "Student")
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "menu:status":
        text, markup = _build_status_payload(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "menu:help":
        help_text = (
            "📖 <b>KL University Assistant Guide</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• <code>/menu</code> — Open interactive dashboard\n"
            "• <code>/timetable [day]</code> — Live classes & rooms\n"
            "• <code>/week</code> — Full weekly timetable schedule\n"
            "• <code>/submissions</code> — LMS tasks & upcoming deadlines\n"
            "• <code>/outpass</code> — Hostel outing status & request\n"
            "• <code>/friend</code> — Look up friend's schedule\n"
            "• <code>/morning</code> — Toggle 7:00 AM daily alerts\n"
            "• <code>/login</code> — Connect ERP student account\n"
            "• <code>/lms_login</code> — Connect Moodle LMS account\n"
            "• <code>/status</code> — Account and integration status\n"
            "• <code>/privacy</code> — Privacy & encryption policy\n"
            "• <code>/logout</code> — Log out ERP and purge data\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        await query.edit_message_text(help_text, parse_mode="HTML", reply_markup=None)
        return


async def handle_timetable_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle day switching, week view, and timetable refreshing from inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    uid = user.id if user else None
    if not uid or not is_authorized(update):
        return

    if data.startswith("tt:day:"):
        target_day = data.split(":", 2)[2]
        html = context.user_data.get("last_timetable_html")
        cached_time = context.user_data.get("last_timetable_time", 0)

        if not html or (time.time() - cached_time > 60):
            try:
                await query.edit_message_text("⚡ <i>Fetching live timetable from ERP...</i>", parse_mode="HTML")
                html = await workload_manager.submit_coalesced_job(
                    (uid, "timetable"),
                    workload_manager.timetable_queue,
                    uid,
                    "fetch_timetable",
                    "timetable",
                    _fetch_live_timetable_job,
                    uid,
                )
                context.user_data["last_timetable_html"] = html
                context.user_data["last_timetable_time"] = time.time()
            except Exception as exc:
                await query.edit_message_text(f"⚠️ <i>{html_lib.escape(security.sanitize_user_error(exc, 'Could not load timetable from ERP.'))}</i>", parse_mode="HTML")
                return

        messages = get_timetable_messages(html, day_filter=target_day)
        kb = get_timetable_keyboard(target_day)
        try:
            await query.edit_message_text(messages[0] if messages else "🏖️ <i>No classes found for this day.</i>", parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    if data.startswith("tt:refresh:"):
        target_day = data.split(":", 2)[2]
        try:
            await query.edit_message_text("⚡ <i>Refreshing live timetable from ERP...</i>", parse_mode="HTML")
            html = await workload_manager.submit_coalesced_job(
                (uid, "timetable"),
                workload_manager.timetable_queue,
                uid,
                "fetch_timetable",
                "timetable",
                _fetch_live_timetable_job,
                uid,
            )
            context.user_data["last_timetable_html"] = html
            context.user_data["last_timetable_time"] = time.time()
            messages = get_timetable_messages(html, day_filter=target_day)
            kb = get_timetable_keyboard(target_day)
            await query.edit_message_text(messages[0] if messages else "🏖️ <i>No classes found for this day.</i>", parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            await query.edit_message_text(f"⚠️ <i>{html_lib.escape(security.sanitize_user_error(exc, 'Failed to refresh timetable from ERP.'))}</i>", parse_mode="HTML")
        return

    if data == "tt:week":
        try:
            await query.edit_message_text("⚡ <i>Fetching live week timetable from ERP...</i>", parse_mode="HTML")
            html = await workload_manager.submit_coalesced_job(
                (uid, "week_timetable"),
                workload_manager.timetable_queue,
                uid,
                "fetch_week_timetable",
                "timetable",
                _fetch_live_timetable_job,
                uid,
            )
            context.user_data["last_timetable_html"] = html
            context.user_data["last_timetable_time"] = time.time()
        except Exception as exc:
            await query.edit_message_text(f"⚠️ <i>{html_lib.escape(security.sanitize_user_error(exc, 'Could not load week timetable from ERP.'))}</i>", parse_mode="HTML")
            return

        messages = get_timetable_messages(html)
        try:
            await query.edit_message_text(messages[0], parse_mode="HTML", reply_markup=None)
            for extra in messages[1:]:
                await context.bot.send_message(chat_id=uid, text=extra, parse_mode="HTML")
        except Exception:
            pass
        return


async def handle_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle interactions from the status dashboard inline buttons."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = update.effective_user
    uid = user.id if user else None
    if not uid or not is_authorized(update):
        return

    if data == "status:refresh":
        text, markup = _build_status_payload(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "status:toggle_morning":
        is_on = storage.is_user_morning_enabled(uid)
        storage.set_user_morning_enabled(uid, not is_on)
        if not is_on:
            _schedule_morning_timetable_job(context)
            await query.answer("🔔 Daily morning alerts enabled at 06:45 AM IST!", show_alert=False)
        else:
            await query.answer("🔕 Daily morning alerts disabled.", show_alert=False)
        text, markup = _build_status_payload(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "status:login":
        context.user_data["login_state"] = STATE_WAITING_USERNAME
        await query.message.reply_text("🔑 Please send your ERP username / Student ID:")
        return

    if data == "status:lms_login":
        context.user_data["login_state"] = STATE_WAITING_LMS_USERNAME
        await query.message.reply_text("📚 Please send your LMS username / Student ID:")
        return

    if data == "status:logout_erp":
        try:
            reset_user_client(uid)
            credentials.delete_user_credentials(uid)
            storage.delete_user_session(uid)
            await query.answer("Logged out of ERP successfully.", show_alert=False)
        except Exception as e:
            logger.error("Error logging out ERP: %s", e)
        text, markup = _build_status_payload(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return

    if data == "status:logout_lms":
        try:
            credentials.delete_user_lms_credentials(uid)
            LMSService.invalidate_user_cache(uid)
            await query.answer("Disconnected LMS account.", show_alert=False)
        except Exception as e:
            logger.error("Error logging out LMS: %s", e)
        text, markup = _build_status_payload(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass
        return


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the privacy policy explaining data collection, storage, and processing."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    privacy_text = (
        "<b>PRIVACY POLICY</b>\n\n"
        "<b>1. Data Storage & Encryption</b>\n"
        "• <b>Telegram User ID:</b> Used as your unique account ID to link your preferences and encrypted sessions.\n"
        "• <b>ERP & LMS Credentials:</b> When you link your accounts via <code>/login</code> or <code>/lms_login</code>, your student usernames, passwords, and API session tokens are encrypted using <b>authenticated Fernet (AES-128-CBC + HMAC-SHA256)</b> before saving to MongoDB. Plaintext passwords are never stored.\n"
        "• <b>Chat Security:</b> Password messages sent in Telegram chat are automatically deleted immediately upon receipt.\n"
        "• <b>Preferences:</b> Stored settings include your daily 7:00 AM morning schedule alert and notification tracking states.\n\n"
        "<b>2. Live Processing & Ephemeral Data</b>\n"
        "• <b>Timetable & LMS Tasks:</b> Schedules and pending assignments are fetched on-demand live from university portals. We do not permanently store your academic records.\n\n"
        "<b>3. Infrastructure</b>\n"
        "• <b>MongoDB Atlas:</b> Encrypted user credentials, session cookies, and settings are stored in an enterprise-grade secured cloud database.\n"
        "• <b>Cloud Deployment:</b> The bot runs as an isolated, secure service.\n"
        "• <b>Telegram:</b> Messages are transmitted via the official Telegram Bot API with content copy protection enabled.\n\n"
        "<b>4. User Control & Data Deletion</b>\n"
        "• <b>/logout:</b> Clears your saved ERP credentials and active sessions from MongoDB.\n"
        "• <b>/lms_logout:</b> Completely purges your saved LMS credentials, tokens, and alert history from MongoDB.\n"
        "• <b>Stopping/Blocking the Bot:</b> Automatically triggers an immediate and permanent purge of all your account data from the database."
    )

    await update.message.reply_text(privacy_text, parse_mode="HTML")


async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send project description and key benefits of using the bot."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    desc_text = (
        "<b>KL University Smart Assistant</b>\n\n"
        "An all-in-one assistant for your ERP academic timetable, LMS assignments, and automated deadline tracking.\n\n"
        "<b>Timetable & Schedule</b>\n"
        "• <b>Live ERP Sync:</b> Fetch your real-time daily (/timetable) or full week (/week) class schedules with subject codes and room numbers.\n"
        "• <b>Morning Dispatch:</b> Wake up to your daily schedule automatically delivered every morning at 7:00 AM IST (/morning).\n"
        "• <b>Offline Access:</b> View previously synced schedules anytime, even during network issues or when university servers are slow.\n\n"
        "<b>LMS Submissions & Deadlines</b>\n"
        "• <b>Submissions Tracker:</b> View all pending Home Assignments, ALMs, and Quizzes grouped neatly with due dates (/submissions).\n"
        "• <b>Automated Reminders:</b> Receive background reminders 2 days (48h) and 6 hours before deadlines so you never miss a submission.\n"
        "• <b>New Task Announcements:</b> Get notified instantly whenever a teacher posts a new assignment or quiz.\n"
        "• <b>1-Tap Submissions:</b> Direct links on alerts take you straight to the submission page.\n\n"
        "<b>Privacy & Security</b>\n"
        "• <b>Fernet Encryption:</b> All credentials and session tokens (ERP and LMS) are encrypted using authenticated AES-128-CBC + HMAC-SHA256 before being stored in MongoDB.\n"
        "• <b>Auto-Delete Passwords:</b> Plaintext passwords entered in chat are deleted instantly.\n"
        "• <b>Full Control:</b> Disconnect and delete your data anytime with /logout and /lms_logout."
    )

    await update.message.reply_text(desc_text, parse_mode="HTML")


async def handle_user_status_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Automatically purge user data from MongoDB when the user stops, blocks, or deletes their chat with the bot."""
    if not update.my_chat_member:
        return

    new_status = update.my_chat_member.new_chat_member.status
    user = update.my_chat_member.from_user
    uid = user.id if user else None

    # Triggered when user blocks or deletes chat with the bot
    if new_status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        if uid:
            try:
                storage.delete_user_all_data(uid)
                logger.info("User %s deleted chat/blocked the bot. Automatically purged stored data from MongoDB.", uid)
            except Exception as exc:
                logger.error("Failed to auto-delete user data on chat deletion for uid=%s: %s", uid, exc)


async def _start_health_server(app: Application) -> None:
    """Start a hardened HTTP health responder if deployed on Render/Cloud (when PORT is set)."""
    import os
    port = os.getenv("PORT")
    if not port:
        return
    try:
        port_num = int(port)
        server = await asyncio.start_server(security.http_health_guard.handle_connection, "0.0.0.0", port_num)
        app.bot_data["_health_server"] = server
        logger.info("Hardened health check server active on port %s for Render/Cloud deployment", port_num)
    except Exception as exc:
        logger.warning("Could not start cloud health check server on port %s: %s", port, exc)


async def _setup_bot_commands(app: Application) -> None:
    """Register active Telegram commands and profile descriptions."""
    try:
        if config.AUTHORIZED_TELEGRAM_USER_IDS:
            for uid in config.AUTHORIZED_TELEGRAM_USER_IDS:
                try:
                    await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id=uid))
                except Exception:
                    pass
            try:
                await app.bot.delete_my_commands(scope=BotCommandScopeDefault())
            except Exception:
                pass
        else:
            await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("Registered %d active Telegram bot commands.", len(BOT_COMMANDS))
    except Exception as exc:
        logger.warning("Could not register bot commands with Telegram: %s", exc)

    try:
        from telegram import MenuButtonCommands
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Configured native Telegram Menu Button.")
    except Exception as exc:
        logger.warning("Could not set chat menu button: %s", exc)

    try:
        bot_desc = (
            "Your smart assistant for KL University ERP & LMS.\n\n"
            "Features:\n"
            "• Real-time ERP timetable (/timetable, /week)\n"
            "• Daily 7:00 AM class schedule (/morning)\n"
            "• LMS pending assignments & deadlines (/submissions)\n"
            "• Automated deadline alerts (48h & 6h reminders)\n"
            "• Instant new task announcements\n"
            "• Hostel e-outing outpass tracking & apply (/outpass)\n"
            "• Friend timetable lookup (/friend)\n"
            "• Authenticated Fernet encryption for credentials"
        )
        bot_short_desc = (
            "Your all-in-one KL University assistant for ERP timetable, LMS submissions, deadlines & morning alerts."
        )
        await app.bot.set_my_description(bot_desc)
        await app.bot.set_my_short_description(bot_short_desc)
        logger.info("Updated Telegram bot descriptions successfully.")
    except Exception as exc:
        logger.warning("Could not set bot profile descriptions: %s", exc)


async def _post_init(app: Application) -> None:
    """Post-initialization hook for cloud health check, command setup, and warmup."""
    await _start_health_server(app)
    await _setup_bot_commands(app)
    # Start the central WorkloadManager and its worker pools + TelegramOutbox
    await workload_manager.start(bot_instance=app.bot)
    # Warmup MongoDB in-memory session/cache preloading
    try:
        loaded = storage.warmup_storage_caches()
        logger.info("Storage caches preloaded into RAM (%d active sessions/preferences).", loaded)
    except Exception as exc:
        logger.warning("Could not pre-warm storage caches: %s", exc)

    # Pre-warm ONNX CAPTCHA solver to eliminate first-login cold-start latency
    try:
        from erp.captcha_solver import warmup_captcha_solver
        warmup_captcha_solver()
        logger.info("ONNX CAPTCHA solver pre-warmed successfully.")
    except Exception as exc:
        logger.warning("Could not pre-warm CAPTCHA solver: %s", exc)


async def _post_shutdown(app: Application) -> None:
    """Graceful shutdown hook to cleanly stop health server, WorkloadManager, and workers."""
    server = app.bot_data.get("_health_server")
    if server:
        try:
            server.close()
            await server.wait_closed()
        except Exception:
            pass
    logger.info("Bot is shutting down, stopping WorkloadManager...")
    await workload_manager.stop()


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(protect_content=True))
        .job_queue(JobQueue())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
    )

    try:
        builder = builder.rate_limiter(AIORateLimiter(max_retries=3))
    except Exception as exc:
        logger.warning("AIORateLimiter optional package not loaded (%s); continuing with security anti-flood middleware.", exc)

    application = builder.build()

    # Register Anti-Flood / Anti-DoS Middleware at group=-1 (executes before all handlers)
    application.add_handler(TypeHandler(Update, security.anti_flood_middleware), group=-1)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("dashboard", menu_command))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("friend", friend))
    application.add_handler(CommandHandler("timetable", timetable))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("submissions", submissions))
    application.add_handler(CommandHandler("lms", submissions))
    application.add_handler(CommandHandler("outpass", outpass_command))
    application.add_handler(CommandHandler("lms_login", lms_login))
    application.add_handler(CommandHandler("lms_logout", lms_logout))
    application.add_handler(CommandHandler("morning", morning))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("description", description))
    application.add_handler(CommandHandler("describe", description))
    application.add_handler(CommandHandler("privacy", privacy))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(handle_timetable_callback, pattern=r"^tt:"))
    application.add_handler(CallbackQueryHandler(handle_status_callback, pattern=r"^status:"))
    application.add_handler(CallbackQueryHandler(handle_lms_callback, pattern=r"^lms:"))
    application.add_handler(CallbackQueryHandler(handle_outpass_callback, pattern=r"^(outpass|outcal)"))
    application.add_handler(ChatMemberHandler(handle_user_status_change, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_message))

    # Schedule the daily morning timetable job for all users with stored credentials.
    _schedule_morning_timetable_job(application)

    # Schedule the periodic LMS deadline & new task alert job.
    _schedule_lms_alerts_job(application)

    # Schedule memory cleanup for rate limiter inactive users and idle locks every 5 minutes.
    async def _cleanup_rate_limiter_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        security.rate_limiter.cleanup_inactive_users()
        security.http_health_guard.cleanup_inactive_ips()
        await workload_manager.user_locks.cleanup_idle_locks()

    if application.job_queue:
        application.job_queue.run_repeating(
            _cleanup_rate_limiter_job,
            interval=300,
            first=300,
            name="rate_limiter_cleanup",
        )

        # Schedule periodic ERP session warmer (refreshes active sessions before expiry)
        async def _session_warmer_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                refreshed = await warm_active_sessions()
                if refreshed > 0:
                    logger.info("Session warmer refreshed %d ERP sessions", refreshed)
            except Exception as exc:
                logger.warning("Session warmer error: %s", exc)

        application.job_queue.run_repeating(
            _session_warmer_job,
            interval=1200,
            first=600,
            name="erp_session_warmer",
        )

    logger.info("KL University Timetable Bot v%s is starting...", BOT_VERSION)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
