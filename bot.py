import asyncio
import logging
import random
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
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    JobQueue,
    MessageHandler,
    filters,
)

import config
import credentials
import storage
from erp.session import perform_login, fetch_timetable_html, load_session
from erp.client import auto_login
from parser.timetable import (
    format_timetable_for_telegram,
    get_timetable_messages,
    _today_day_name,
    resolve_day_name,
)
from lms.client import LMSClient
from lms.service import LMSService

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

BOT_VERSION = "5.3.0"

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
    BotCommand("timetable", "Today's classes, timings & rooms"),
    BotCommand("week", "Full weekly timetable view"),
    BotCommand("friend", "Check your friend's timetable"),
    BotCommand("submissions", "Pending assignments & LMS deadlines"),
    BotCommand("login", "Connect & save ERP credentials"),
    BotCommand("lms_login", "Connect & save LMS credentials"),
    BotCommand("lms_logout", "Disconnect LMS credentials"),
    BotCommand("morning", "Sends the day's schedule daily at 7 AM"),
    BotCommand("status", "Check ERP connection status"),
    BotCommand("description", "Why use this bot & key benefits"),
    BotCommand("privacy", "View bot privacy policy"),
    BotCommand("logout", "Log out ERP & clear your data"),
]


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

    welcome_text = (
        "🎓 <b>Your KL University Assistant welcomes you!</b>\n\n"
        "Timetable, ERP schedule &amp; LMS pending submissions — all in one place.\n\n"
        "✨ <b>What you can do:</b>\n\n"
        "📅 Check today's classes &amp; room numbers (<code>/timetable</code>)\n\n"
        "🗓️ View your full weekly timetable (<code>/week</code>)\n\n"
        "📋 <b>Track pending LMS HAs, ALMs &amp; deadlines</b> (<code>/submissions</code>)\n\n"
        "⏰ Get daily morning schedule alerts (<code>/morning</code>)\n\n"
        "👥 Check your friends' schedules (<code>/friend</code>)\n\n"
        "👇 Tap the Menu button below ( or ) Type  /  to view commands"
    )

    if update.message:
        await update.message.reply_text(
            welcome_text,
            parse_mode="HTML",
        )


async def _run_login(update: Update, username: str, password: str) -> None:
    """Run the async ERP login and report the result."""
    await update.message.reply_text("⏳ Logging in to ERP...")
    user = update.effective_user
    uid = user.id if user else None
    try:
        await perform_login(username, password, user_id=uid)

        # Save credentials for this Telegram user for future automatic use.
        if uid:
            credentials.save_user_credentials(uid, username, password)

        await update.message.reply_text(
            "✅ <b>Logged in successfully.</b>\n"
            "Send /timetable to get your timetable.",
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.exception("Login failed")
        await update.message.reply_text(
            "❌ <b>Login failed.</b> Please check your credentials and try again.",
            parse_mode="HTML"
        )


async def _run_friend_login(update: Update, username: str, password: str) -> None:
    """Log in with another user's credentials and send their timetable without saving them."""
    await update.message.reply_text("⏳ Fetching friend's timetable...")
    try:
        # Perform ephemeral login without saving to user's persistent session
        login_res = await auto_login(username, password)
        cookies = login_res["cookies"]
        csrf = login_res.get("csrf_token", "")
        html = await fetch_timetable_html(cookies=cookies, csrf_token=csrf)
        message = format_timetable_for_telegram(html, day=_today_day_name())

        await update.message.reply_text(
            message,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Friend login failed")
        await update.message.reply_text(
            "❌ <b>Could not fetch timetable.</b> Please check the credentials and try again.",
            parse_mode="HTML"
        )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
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

    state = context.user_data.get("login_state")
    text = update.message.text.strip()
    is_friend_login = context.user_data.get("friend_login", False)

    if text.lower() in {"cancel", "/cancel"}:
        context.user_data.pop("login_state", None)
        context.user_data.pop("friend_login", None)
        context.user_data.pop("erp_username", None)
        context.user_data.pop("lms_username", None)
        await update.message.reply_text("❌ Action cancelled.")
        return

    # --- ERP / Friend Login Flow ---
    if state == STATE_WAITING_USERNAME:
        context.user_data["erp_username"] = text
        context.user_data["login_state"] = STATE_WAITING_PASSWORD
        await update.message.reply_text("Send your ERP password.\n<i>(Your message will be automatically deleted)</i>", parse_mode="HTML")
        return

    if state == STATE_WAITING_PASSWORD:
        username = context.user_data.pop("erp_username", "")
        context.user_data.pop("login_state", None)
        context.user_data.pop("friend_login", None)
        password = text

        # Immediately delete the password message from Telegram chat for user privacy
        try:
            await update.message.delete()
        except Exception:
            pass

        if not username:
            await update.message.reply_text("⚠️ Something went wrong. Start again.")
            return

        if is_friend_login:
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
    """Fetch pending tasks from LMS and display the interactive card."""
    try:
        tasks = await LMSService.fetch_all_pending_tasks(user_id, force_refresh=force_refresh)
        _, student_name = await LMSService.get_authenticated_client(user_id)
        formatted_text = LMSService.format_tasks_telegram_message(tasks, student_name=student_name)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh Tasks", callback_data="lms:refresh"),
                InlineKeyboardButton("🚪 Disconnect LMS", callback_data="lms:logout"),
            ]
        ])

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

        if message_to_edit:
            await message_to_edit.edit_text(
                formatted_text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        else:
            await update.effective_message.reply_text(
                formatted_text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        logger.exception("Error fetching pending tasks for user %s: %s", user_id, exc)
        err_msg = (
            "⚠️ <b>Could not fetch LMS tasks.</b>\n"
            f"<i>{str(exc)}</i>\n\n"
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

    saved_lms = credentials.load_user_lms_credentials(user.id)
    if not saved_lms:
        # Submissions are strictly visible only if the user is logged into LMS
        await update.message.reply_text(
            "<b>LMS Not Connected</b>\n\n"
            "Please connect your KL LMS account first using /lms_login to view your pending submissions and deadlines.",
            parse_mode="HTML",
        )
        return

    # Check for warm in-memory cache for ultra-fast instant rendering
    now = time.time()
    cached = LMSService._task_cache.get(user.id)
    if cached and (now - cached[0] < 120):
        await _send_lms_pending_tasks(update, context, user.id, force_refresh=False)
        return

    status_msg = await update.message.reply_text("⏳ <b>Fetching your submissions from LMS...</b>", parse_mode="HTML")
    await _send_lms_pending_tasks(update, context, user.id, message_to_edit=status_msg, force_refresh=False)


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
        await _send_lms_pending_tasks(update, context, user.id, message_to_edit=query.message, force_refresh=True)
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
                    btn_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open LMS Submission", url=task.url)]]) if task.url else None
                    try:
                        await context.bot.send_message(
                            chat_id=uid,
                            text=alert_text,
                            parse_mode="HTML",
                            reply_markup=btn_kb,
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
                        btn_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open LMS Submission", url=task.url)]]) if task.url else None
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=alert_text,
                                parse_mode="HTML",
                                reply_markup=btn_kb,
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
                        btn_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Submit on LMS Now", url=task.url)]]) if task.url else None
                        try:
                            await context.bot.send_message(
                                chat_id=uid,
                                text=alert_text,
                                parse_mode="HTML",
                                reply_markup=btn_kb,
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

    status_msg = await update.message.reply_text("⏳ Fetching your timetable...")
    user = update.effective_user
    uid = user.id if user else None

    try:
        try:
            html = await fetch_timetable_html(user_id=uid)
        except Exception:
            # Silent re-auth fallback
            saved = credentials.load_user_credentials(uid)
            if saved:
                await perform_login(saved["username"], saved["password"], user_id=uid)
                html = await fetch_timetable_html(user_id=uid)
            else:
                raise

        messages = get_timetable_messages(html, day_filter=day)
        if not messages:
            await status_msg.edit_text("⚠️ <i>Could not extract timetable. Please check in ERP.</i>", parse_mode="HTML")
            return

        await status_msg.edit_text(
            messages[0],
            parse_mode="HTML",
        )
        for extra_msg in messages[1:]:
            await update.message.reply_text(extra_msg, parse_mode="HTML")
    except Exception as exc:
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

    status_msg = await update.message.reply_text("⏳ Fetching your full week timetable...")
    user = update.effective_user
    uid = user.id if user else None

    try:
        try:
            html = await fetch_timetable_html(user_id=uid)
        except Exception:
            # Silent re-auth fallback
            saved = credentials.load_user_credentials(uid)
            if saved:
                await perform_login(saved["username"], saved["password"], user_id=uid)
                html = await fetch_timetable_html(user_id=uid)
            else:
                raise

        messages = get_timetable_messages(html)
        if not messages:
            await status_msg.edit_text("⚠️ <i>Could not extract timetable. Please check in ERP.</i>", parse_mode="HTML")
            return

        await status_msg.edit_text(
            messages[0],
            parse_mode="HTML",
        )
        for extra_msg in messages[1:]:
            await update.message.reply_text(extra_msg, parse_mode="HTML")
    except Exception as exc:
        logger.exception("Failed to fetch week timetable")
        await status_msg.edit_text(
            "⚠️ <b>Unable to fetch live timetable from ERP.</b>\n"
            "Please check directly in the ERP portal or try again later.",
            parse_mode="HTML",
        )



async def friend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch another user's timetable by entering their ERP credentials."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    context.user_data["friend_login"] = True
    context.user_data["login_state"] = STATE_WAITING_USERNAME
    await update.message.reply_text(
        "Send the ERP username / Student ID of the friend whose timetable you want to see."
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    if uid:
        try:
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


async def _prefetch_morning_timetables(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background warmup job (starts at 06:30 IST): pre-authenticates and pre-fetches timetables without rushing."""
    morning_uids = storage.get_all_morning_users()
    if not morning_uids:
        return

    logger.info("Starting gentle morning pre-fetch warmup for %d users...", len(morning_uids))
    _morning_cache.clear()
    failed_users: list[tuple[int, dict]] = []

    for chat_id in morning_uids:
        saved = credentials.load_user_credentials(chat_id)
        if not saved:
            continue

        try:
            try:
                html = await fetch_timetable_html(user_id=chat_id)
            except Exception:
                # Gentle login attempt
                await perform_login(saved["username"], saved["password"], user_id=chat_id, max_retries=3)
                html = await fetch_timetable_html(user_id=chat_id)

            _morning_cache[chat_id] = html
            logger.info("Pre-fetch successful for user %s", chat_id)
        except Exception:
            logger.warning("Pre-fetch initial attempt failed for user %s, queued for retry", chat_id)
            failed_users.append((chat_id, saved))
        finally:
            # Space out between users to never flood the ERP
            await asyncio.sleep(random.uniform(2.5, 4.5))

    # Gentle retry pass for failed users after a 10-second rest
    if failed_users:
        logger.info("Retrying pre-fetch for %d users after cool-down...", len(failed_users))
        await asyncio.sleep(10.0)
        for chat_id, saved in failed_users:
            try:
                await perform_login(saved["username"], saved["password"], user_id=chat_id, max_retries=3)
                html = await fetch_timetable_html(user_id=chat_id)
                _morning_cache[chat_id] = html
                logger.info("Pre-fetch retry successful for user %s", chat_id)
            except Exception:
                logger.warning("Pre-fetch retry failed for user %s", chat_id)
            finally:
                await asyncio.sleep(random.uniform(3.0, 5.0))


async def _send_morning_timetable(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: send today's timetable to every user with morning alert enabled (06:45 AM - 07:30 AM)."""
    morning_uids = storage.get_all_morning_users()
    if not morning_uids:
        logger.info("Morning timetable skipped: no users with morning alerts enabled.")
        return

    today_name = _today_day_name()
    for chat_id in morning_uids:
        saved = credentials.load_user_credentials(chat_id)
        if not saved:
            continue

        html = _morning_cache.get(chat_id)

        if not html:
            try:
                try:
                    html = await fetch_timetable_html(user_id=chat_id)
                except Exception:
                    # Single gentle re-login attempt
                    await perform_login(saved["username"], saved["password"], user_id=chat_id, max_retries=3)
                    html = await fetch_timetable_html(user_id=chat_id)
            except Exception:
                logger.warning("Live morning timetable fetch failed for chat_id=%s", chat_id)
                html = None

        if html:
            try:
                message = format_timetable_for_telegram(html, day=today_name)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🌅 <b>Good morning! Here is today's schedule ({today_name}):</b>\n\n{message}",
                    parse_mode="HTML",
                )
                logger.info("Morning timetable sent successfully to chat_id=%s", chat_id)
            except Exception:
                logger.exception("Error formatting or sending morning timetable to chat_id=%s", chat_id)
        else:
            logger.error("Unable to fetch live timetable for chat_id=%s", chat_id)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ <b>Unable to fetch live timetable from ERP this morning.</b>\nPlease check directly in the ERP portal.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Space out deliveries nicely between users (1.5 - 3.5 seconds)
        await asyncio.sleep(random.uniform(1.5, 3.5))

    # Clear memory cache for the next day
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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    session = load_session(user_id=uid)
    
    if session and session.get("cookies"):
        await update.message.reply_text("✅ <b>Logged In:</b> Your ERP session is active.", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ <b>Logged Out:</b> Use /login to connect your ERP account.", parse_mode="HTML")


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
        "• <b>Timetable & LMS Tasks:</b> Schedules and pending assignments are fetched on-demand live from university portals. We do not permanently store your academic records.\n"
        "• <b>Friend Feature (/friend):</b> 100% ephemeral. Credentials entered for a friend are processed in memory for a single fetch and are never stored in the database.\n\n"
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
        "• <b>Offline Access:</b> View previously synced schedules anytime, even during network issues or when university servers are slow.\n"
        "• <b>Friend Timetable:</b> Check your classmate's schedule on the fly without logging out (/friend).\n\n"
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
    """Start a lightweight HTTP health responder if deployed on Render/Cloud (when PORT is set)."""
    import os
    port = os.getenv("PORT")
    if not port:
        return
    try:
        port_num = int(port)

        async def handle_ping(reader, writer):
            try:
                await reader.read(1024)
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"OK"
                )
                writer.write(response)
                await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_ping, "0.0.0.0", port_num)
        app.bot_data["_health_server"] = server
        logger.info("Health check server active on port %s for Render/Cloud deployment", port_num)
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
        bot_desc = (
            "Your smart assistant for KL University ERP & LMS.\n\n"
            "Features:\n"
            "• Real-time ERP timetable (/timetable, /week)\n"
            "• Daily 7:00 AM class schedule (/morning)\n"
            "• LMS pending assignments & deadlines (/submissions)\n"
            "• Automated deadline alerts (48h & 6h reminders)\n"
            "• Instant new task announcements\n"
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
    """Post-initialization hook for cloud health check and command setup."""
    await _start_health_server(app)
    await _setup_bot_commands(app)


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(protect_content=True))
        .job_queue(JobQueue())
        .post_init(_post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("timetable", timetable))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("friend", friend))
    application.add_handler(CommandHandler("submissions", submissions))
    application.add_handler(CommandHandler("lms", submissions))
    application.add_handler(CommandHandler("lms_login", lms_login))
    application.add_handler(CommandHandler("lms_logout", lms_logout))
    application.add_handler(CommandHandler("morning", morning))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("description", description))
    application.add_handler(CommandHandler("describe", description))
    application.add_handler(CommandHandler("privacy", privacy))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CallbackQueryHandler(handle_lms_callback, pattern=r"^lms:"))
    application.add_handler(ChatMemberHandler(handle_user_status_change, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_message))

    # Schedule the daily morning timetable job for all users with stored credentials.
    _schedule_morning_timetable_job(application)

    # Schedule the periodic LMS deadline & new task alert job.
    _schedule_lms_alerts_job(application)

    logger.info("KL University Timetable Bot v%s is starting...", BOT_VERSION)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
