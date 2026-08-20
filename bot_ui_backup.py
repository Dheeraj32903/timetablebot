import asyncio
import logging
import re
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional, Tuple

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
from erp.client import auto_login, fetch_timetable
from parser.timetable import (
    format_timetable_for_telegram,
    _today_day_name,
    resolve_day_name,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states for collecting credentials in Telegram chat.
STATE_WAITING_USERNAME = "waiting_username"
STATE_WAITING_PASSWORD = "waiting_password"

BOT_VERSION = "5.1.0"

# Daily automatic timetable message job name.
MORNING_TIMETABLE_JOB_NAME = "morning_timetable"

# Ordered days for timetable navigation
DAYS_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# In-memory cache for timetable HTML to provide sub-10ms response times
# user_id -> (html_content, fetch_timestamp)
_timetable_cache: dict[int, tuple[str, float]] = {}
CACHE_TTL_SECONDS = 900  # 15 minutes TTL


def _get_cached_timetable_html(user_id: Optional[int]) -> Optional[str]:
    """Retrieve cached timetable HTML if still fresh."""
    if not user_id or user_id not in _timetable_cache:
        return None
    html, timestamp = _timetable_cache[user_id]
    if time.time() - timestamp > CACHE_TTL_SECONDS:
        _timetable_cache.pop(user_id, None)
        return None
    return html


def _set_cached_timetable_html(user_id: Optional[int], html: str) -> None:
    """Store timetable HTML in cache."""
    if user_id and html:
        _timetable_cache[user_id] = (html, time.time())


def _clear_cached_timetable_html(user_id: Optional[int] = None) -> None:
    """Clear timetable cache for a specific user or all users."""
    if user_id:
        _timetable_cache.pop(user_id, None)
    else:
        _timetable_cache.clear()


def _clean_user_display_name(raw_name: Optional[str]) -> str:
    """Clean up Telegram names like '2400032903 _Dheeraj' -> 'Dheeraj'."""
    if not raw_name:
        return "Student"
    name = raw_name.strip()
    match = re.search(r"^\d+[\s_\-]+([A-Za-z]+.*)$", name)
    if match:
        return match.group(1).strip()
    return name


# ---------------------------------------------------------------------------
# UI Keyboards & Menu Builders
# ---------------------------------------------------------------------------

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard for quick one-tap actions."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📅 Today's Schedule"), KeyboardButton("📆 Full Week")],
            [KeyboardButton("🗓️ Pick Day"), KeyboardButton("👥 Friend Schedule")],
            [KeyboardButton("⏰ Morning Alert"), KeyboardButton("⚙️ Account")],
            [KeyboardButton("ℹ️ Status"), KeyboardButton("❓ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_main_inline_keyboard(is_logged_in: bool = False, morning_enabled: bool = False) -> InlineKeyboardMarkup:
    """Main interactive menu attached to /start or menu cards."""
    auth_btn_text = "⚙️ Account" if is_logged_in else "🔑 Log In to ERP"
    morning_btn_text = "🔕 Alert: ON" if morning_enabled else "🔔 Alert: OFF"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 Today's Schedule", callback_data="tt:day:today"),
                InlineKeyboardButton("📆 Full Week", callback_data="tt:week"),
            ],
            [
                InlineKeyboardButton("🗓️ Pick Day", callback_data="tt:picker"),
                InlineKeyboardButton("👥 Friend Schedule", callback_data="friend:start"),
            ],
            [
                InlineKeyboardButton(morning_btn_text, callback_data="morning:toggle"),
                InlineKeyboardButton(auth_btn_text, callback_data="auth:menu"),
            ],
        ]
    )


def get_day_selector_keyboard() -> InlineKeyboardMarkup:
    """Inline grid for selecting a specific day of the week."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Mon", callback_data="tt:day:Mon"),
                InlineKeyboardButton("Tue", callback_data="tt:day:Tue"),
                InlineKeyboardButton("Wed", callback_data="tt:day:Wed"),
            ],
            [
                InlineKeyboardButton("Thu", callback_data="tt:day:Thu"),
                InlineKeyboardButton("Fri", callback_data="tt:day:Fri"),
                InlineKeyboardButton("Sat", callback_data="tt:day:Sat"),
            ],
            [
                InlineKeyboardButton("◀️ Yesterday", callback_data="tt:day:yesterday"),
                InlineKeyboardButton("📅 Today", callback_data="tt:day:today"),
                InlineKeyboardButton("Tomorrow ▶️", callback_data="tt:day:tomorrow"),
            ],
            [
                InlineKeyboardButton("📆 Full Week", callback_data="tt:week"),
                InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main"),
            ],
        ]
    )


def get_timetable_nav_keyboard(current_day: Optional[str] = None) -> InlineKeyboardMarkup:
    """Interactive navigation buttons attached under a timetable message."""
    if current_day and current_day in DAYS_ORDER:
        idx = DAYS_ORDER.index(current_day)
        prev_day = DAYS_ORDER[(idx - 1) % len(DAYS_ORDER)]
        next_day = DAYS_ORDER[(idx + 1) % len(DAYS_ORDER)]
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"◀️ {prev_day}", callback_data=f"tt:day:{prev_day}"),
                    InlineKeyboardButton("📅 Today", callback_data="tt:day:today"),
                    InlineKeyboardButton(f"{next_day} ▶️", callback_data=f"tt:day:{next_day}"),
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"tt:refresh:{current_day}"),
                    InlineKeyboardButton("🗓️ Pick Day", callback_data="tt:picker"),
                    InlineKeyboardButton("📆 Full Week", callback_data="tt:week"),
                ],
                [
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main"),
                ],
            ]
        )
    elif current_day == "Sun":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("◀️ Sat", callback_data="tt:day:Sat"),
                    InlineKeyboardButton("📅 Today", callback_data="tt:day:today"),
                    InlineKeyboardButton("Mon ▶️", callback_data="tt:day:Mon"),
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="tt:refresh:Sun"),
                    InlineKeyboardButton("🗓️ Pick Day", callback_data="tt:picker"),
                    InlineKeyboardButton("📆 Full Week", callback_data="tt:week"),
                ],
                [
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main"),
                ],
            ]
        )
    else:
        # Full week view
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📅 Today", callback_data="tt:day:today"),
                    InlineKeyboardButton("🗓️ Pick Day", callback_data="tt:picker"),
                    InlineKeyboardButton("🔄 Refresh", callback_data="tt:refresh:week"),
                ],
                [
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main"),
                ],
            ]
        )


def get_account_inline_keyboard(is_logged_in: bool) -> InlineKeyboardMarkup:
    """Account & login management options."""
    if is_logged_in:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("ℹ️ Connection Status", callback_data="auth:status"),
                    InlineKeyboardButton("🔄 Re-login", callback_data="auth:login"),
                ],
                [
                    InlineKeyboardButton("🚪 Log Out", callback_data="auth:logout"),
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main"),
                ],
            ]
        )
    else:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔑 Log In to ERP", callback_data="auth:login"),
                ],
                [
                    InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main"),
                ],
            ]
        )


def get_cancel_inline_keyboard() -> InlineKeyboardMarkup:
    """Inline button to abort an ongoing prompt."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel:action")],
        ]
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


async def _fetch_timetable_content(
    user_id: Optional[int],
    day: Optional[str] = None,
    force_refresh: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch and format timetable HTML.
    Uses in-memory cache for blazing fast responses unless force_refresh is True.
    Handles per-user auto-login seamlessly if session is missing or expired.
    """
    # 1. Try cache first if not forced refresh
    if not force_refresh:
        cached_html = _get_cached_timetable_html(user_id)
        if cached_html:
            try:
                msg = format_timetable_for_telegram(cached_html, day=day)
                return msg, None
            except Exception as exc:
                logger.warning("Error formatting cached timetable: %s", exc)

    # 2. Try fetching fresh HTML from ERP using user's existing session
    try:
        html = await fetch_timetable_html(user_id=user_id)
        _set_cached_timetable_html(user_id, html)
        msg = format_timetable_for_telegram(html, day=day)
        return msg, None
    except Exception as exc:
        logger.info("Direct timetable fetch failed for user %s (%s), attempting silent auto-login...", user_id, exc)

    # 3. Attempt silent auto-login using saved credentials
    saved = credentials.load_user_credentials(user_id) if user_id else None
    username = saved["username"] if saved else config.ERP_USERNAME
    password = saved["password"] if saved else config.ERP_PASSWORD

    if username and password:
        try:
            await perform_login(username, password, user_id=user_id)
            html = await fetch_timetable_html(user_id=user_id)
            _set_cached_timetable_html(user_id, html)
            msg = format_timetable_for_telegram(html, day=day)
            return msg, None
        except Exception as exc:
            logger.exception("Auto-login during timetable fetch failed for user %s", user_id)
            return None, "Login session expired and auto-login was unsuccessful.\nPlease tap /login to sign in again."

    return None, "You are not logged in. Please tap /login to sign in to your ERP account."


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        if update.message:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    saved = credentials.load_user_credentials(uid) if uid else None
    is_logged_in = bool(saved or (config.ERP_USERNAME and config.ERP_PASSWORD))
    morning_on = _is_morning_enabled(uid)

    user_name = _clean_user_display_name(user.first_name if user else None)
    student_id = saved["username"] if saved else ("Configured" if is_logged_in else "Not Logged In")
    status_text = "Active & Ready ✅" if is_logged_in else "Not Logged In ❌"
    morning_text = "07:00 AM IST (Active 🔔)" if morning_on else "Disabled 🔕"

    welcome_text = (
        f"👋 <b>Welcome, {user_name}!</b>\n\n"
        f"<blockquote>🎓 <b>UniTable • KL University</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Student ID:</b> <code>{student_id}</code>\n"
        f"⚡ <b>Status:</b> <b>{status_text}</b>\n"
        f"⏰ <b>Morning Alert:</b> <b>{morning_text}</b></blockquote>\n\n"
        f"👇 <b>Select an option below to get started:</b>"
    )

    inline_markup = get_main_inline_keyboard(is_logged_in=is_logged_in, morning_enabled=morning_on)

    if update.message:
        await update.message.reply_text(
            welcome_text,
            parse_mode="HTML",
            reply_markup=inline_markup,
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                welcome_text,
                parse_mode="HTML",
                reply_markup=inline_markup,
            )
        except BadRequest:
            pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        if update.message:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    help_text = (
        "<blockquote>🤖 <b>UniTable — Quick Help Guide</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "• 📅 <b>Today's Schedule:</b> View your daily classes\n"
        "• 📆 <b>Full Week:</b> View the entire weekly schedule\n"
        "• 🗓️ <b>Pick Day:</b> Select any specific day (Mon–Sat)\n"
        "• 👥 <b>Friend Schedule:</b> Check another student's timetable\n"
        "• ⏰ <b>Morning Alert:</b> Automated daily 7 AM IST alert\n"
        "• ⚙️ <b>Account:</b> Manage login and credentials</blockquote>\n\n"
        "<b>Available Commands:</b>\n"
        "/start • /timetable • /week • /friend • /morning • /status • /logout • /help"
    )
    if update.message:
        await update.message.reply_text(
            help_text,
            parse_mode="HTML",
            reply_markup=get_main_inline_keyboard(),
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort any active input state."""
    context.user_data.pop("login_state", None)
    context.user_data.pop("friend_login", None)
    context.user_data.pop("erp_username", None)

    if update.message:
        await update.message.reply_text(
            "❌ Action cancelled.",
            reply_markup=get_main_reply_keyboard(),
        )


async def _run_login(update: Update, username: str, password: str) -> None:
    """Run the async ERP login and report the result."""
    msg = await update.message.reply_text("⏳ Logging in to ERP with CAPTCHA solver...")
    user = update.effective_user
    uid = user.id if user else None
    try:
        await perform_login(username, password, user_id=uid)

        # Save credentials for this Telegram user for future automatic use.
        if uid:
            credentials.save_user_credentials(uid, username, password)
            _clear_cached_timetable_html(uid)

        morning_on = _is_morning_enabled(uid)
        await msg.edit_text(
            "<blockquote>✅ <b>Logged in successfully!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Student ID:</b> <code>{username}</code>\n"
            "🔒 <b>Security:</b> Credentials encrypted & persistent in database</blockquote>\n\n"
            "Tap below to view your timetable:",
            parse_mode="HTML",
            reply_markup=get_main_inline_keyboard(is_logged_in=True, morning_enabled=morning_on),
        )
    except Exception as exc:
        logger.exception("Login failed")
        await msg.edit_text(
            "<blockquote>❌ <b>Login failed.</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Please check your username and password, then try again.</blockquote>",
            parse_mode="HTML",
            reply_markup=get_account_inline_keyboard(False),
        )


async def _run_friend_login(update: Update, username: str, password: str) -> None:
    """Log in with another user's credentials and send their timetable without touching stored sessions."""
    msg = await update.message.reply_text("⏳ Logging in and fetching friend's timetable...")
    try:
        # Perform ephemeral login without overwriting the user's saved session
        login_res = await auto_login(username, password)
        cookies = login_res["cookies"]
        csrf = login_res.get("csrf_token", "")
        html = await fetch_timetable_html(cookies=cookies, csrf_token=csrf)
        today_name = _today_day_name()
        result_message = format_timetable_for_telegram(html, day=today_name)

        await msg.edit_text(
            f"👥 <b>Timetable for {username} ({today_name}):</b>\n\n{result_message}",
            parse_mode="HTML",
            reply_markup=get_timetable_nav_keyboard(today_name),
        )
    except Exception as exc:
        logger.exception("Friend login failed")
        await msg.edit_text(
            "❌ <b>Could not fetch friend's timetable.</b>\n"
            "Please verify the credentials and try again.",
            parse_mode="HTML",
            reply_markup=get_main_inline_keyboard(),
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
    context.user_data.pop("friend_login", None)
    await update.message.reply_text(
        "<blockquote>🔑 <b>ERP Login (Step 1 of 2)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Please send your ERP <b>Username / Student ID</b>:</blockquote>",
        parse_mode="HTML",
        reply_markup=get_cancel_inline_keyboard(),
    )


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    arg = " ".join(context.args).strip() if context.args else ""
    day = resolve_day_name(arg)
    if day is None:
        await update.message.reply_text(
            "⚠️ Invalid day. Please select a day from the grid below:",
            parse_mode="HTML",
            reply_markup=get_day_selector_keyboard(),
        )
        return

    status_msg = await update.message.reply_text(f"⏳ Fetching schedule for <b>{day}</b>...", parse_mode="HTML")

    user = update.effective_user
    msg, err = await _fetch_timetable_content(user.id if user else None, day=day)

    if err:
        await status_msg.edit_text(
            f"❌ {err}",
            parse_mode="HTML",
            reply_markup=get_account_inline_keyboard(False),
        )
        return

    await status_msg.edit_text(
        msg,
        parse_mode="HTML",
        reply_markup=get_timetable_nav_keyboard(day),
    )


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    status_msg = await update.message.reply_text("⏳ Fetching full week timetable...", parse_mode="HTML")

    user = update.effective_user
    msg, err = await _fetch_timetable_content(user.id if user else None, day=None)

    if err:
        await status_msg.edit_text(
            f"❌ {err}",
            parse_mode="HTML",
            reply_markup=get_account_inline_keyboard(False),
        )
        return

    await status_msg.edit_text(
        msg,
        parse_mode="HTML",
        reply_markup=get_timetable_nav_keyboard(None),
    )


async def friend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch another user's timetable by entering their ERP credentials."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    context.user_data["friend_login"] = True
    context.user_data["login_state"] = STATE_WAITING_USERNAME
    await update.message.reply_text(
        "<blockquote>👥 <b>Friend's Timetable (Step 1 of 2)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Please send the ERP <b>username / ID</b> of the student whose timetable you want to view:</blockquote>",
        parse_mode="HTML",
        reply_markup=get_cancel_inline_keyboard(),
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if user:
        credentials.delete_user_credentials(user.id)
        storage.delete_user_session(user.id)
        _clear_cached_timetable_html(user.id)

    await update.message.reply_text(
        "<blockquote>🚪 <b>Logged Out</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Your saved credentials and session have been cleared.</blockquote>\n\n"
        "Tap below if you wish to sign in again:",
        parse_mode="HTML",
        reply_markup=get_account_inline_keyboard(False),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    saved = credentials.load_user_credentials(uid) if uid else None
    session = load_session(user_id=uid)
    has_session = bool(session and session.get("cookies"))
    has_saved = bool(saved or (config.ERP_USERNAME and config.ERP_PASSWORD))
    db_type = "MongoDB Atlas 🍃" if storage.is_mongodb_active() else "Local SQLite 🗄️"

    user_info = f"👤 <b>Account ID:</b> <code>{saved['username']}</code>\n" if saved else ""
    session_status = "Active & Ready ✅" if has_session else "Inactive / Stored ⏳"
    morning_status = "Active (7:00 AM IST) 🔔" if _is_morning_enabled(uid) else "Disabled 🔕"

    status_text = (
        f"<blockquote>ℹ️ <b>UniTable • System Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{user_info}"
        f"🔑 <b>Credentials:</b> {'Saved & Encrypted ✅' if has_saved else 'Not Saved ❌'}\n"
        f"🌐 <b>ERP Session:</b> <b>{session_status}</b>\n"
        f"💾 <b>Database:</b> <b>{db_type}</b>\n"
        f"⏰ <b>Morning Alert:</b> <b>{morning_status}</b></blockquote>"
    )

    await update.message.reply_text(
        status_text,
        parse_mode="HTML",
        reply_markup=get_account_inline_keyboard(has_saved),
    )


async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the daily morning timetable message for the calling user."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    if not user:
        return
    saved = credentials.load_user_credentials(user.id)
    if not saved and not config.ERP_USERNAME:
        await update.message.reply_text(
            "⚠️ No stored credentials found. Please log in with /login first.",
            reply_markup=get_account_inline_keyboard(False),
        )
        return

    is_enabled = _is_morning_enabled(user.id)
    if is_enabled:
        _set_morning_enabled(False, user.id)
        if not _is_morning_enabled():
            if context.job_queue:
                for job in context.job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME):
                    job.schedule_removal()
        await update.message.reply_text(
            "🔕 <b>Daily 7 AM morning alert disabled.</b>",
            parse_mode="HTML",
            reply_markup=get_main_inline_keyboard(is_logged_in=True, morning_enabled=False),
        )
    else:
        _set_morning_enabled(True, user.id)
        _schedule_morning_timetable_job(context)
        await update.message.reply_text(
            f"🔔 <b>Morning alert enabled!</b>\n"
            f"You will receive your schedule daily at "
            f"<b>{config.MORNING_TIMETABLE_HOUR:02d}:{config.MORNING_TIMETABLE_MINUTE:02d} IST</b>.",
            parse_mode="HTML",
            reply_markup=get_main_inline_keyboard(is_logged_in=True, morning_enabled=True),
        )


# ---------------------------------------------------------------------------
# Background Jobs
# ---------------------------------------------------------------------------

async def _send_morning_timetable(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: send today's timetable to every user with morning alert enabled."""
    morning_uids = storage.get_all_morning_users()
    if not morning_uids:
        logger.info("Morning timetable skipped: no users with morning alerts enabled.")
        return

    today_name = _today_day_name()
    for chat_id in morning_uids:
        saved = credentials.load_user_credentials(chat_id)
        if not saved:
            continue

        try:
            try:
                html = await fetch_timetable_html(user_id=chat_id)
            except Exception:
                await perform_login(saved["username"], saved["password"], user_id=chat_id)
                html = await fetch_timetable_html(user_id=chat_id)

            _set_cached_timetable_html(chat_id, html)
            message = format_timetable_for_telegram(html, day=today_name)

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🌅 <b>Good morning! Here is today's schedule ({today_name}):</b>\n\n{message}",
                parse_mode="HTML",
                reply_markup=get_timetable_nav_keyboard(today_name),
            )
        except Exception:
            logger.exception("Failed to send morning timetable to chat_id=%s", chat_id)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Could not fetch your morning timetable. Try /login and then /timetable.",
                    reply_markup=get_account_inline_keyboard(False),
                )
            except Exception:
                pass


def _schedule_morning_timetable_job(context_or_app) -> None:
    """Schedule the daily morning timetable job if enabled and not already scheduled."""
    if not _is_morning_enabled():
        return

    job_queue = getattr(context_or_app, "job_queue", None)
    if not job_queue:
        return

    current_jobs = job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME)
    if current_jobs:
        return

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = None

    scheduled_time = dt_time(
        hour=config.MORNING_TIMETABLE_HOUR,
        minute=config.MORNING_TIMETABLE_MINUTE,
        tzinfo=tz,
    )

    job_queue.run_daily(
        _send_morning_timetable,
        time=scheduled_time,
        name=MORNING_TIMETABLE_JOB_NAME,
    )
    logger.info(
        "Scheduled morning timetable job at %02d:%02d IST",
        config.MORNING_TIMETABLE_HOUR,
        config.MORNING_TIMETABLE_MINUTE,
    )


# ---------------------------------------------------------------------------
# Callback Query Router (Interactive Buttons)
# ---------------------------------------------------------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button taps."""
    query = update.callback_query
    if not query:
        return

    if not is_authorized(update):
        await query.answer("⛔ You are not authorized to use this bot.", show_alert=True)
        return

    data = query.data or ""
    user = update.effective_user
    user_id = user.id if user else None

    # Main menu
    if data == "menu:main":
        await query.answer()
        saved = credentials.load_user_credentials(user_id) if user_id else None
        is_logged_in = bool(saved or (config.ERP_USERNAME and config.ERP_PASSWORD))
        morning_on = _is_morning_enabled(user_id)

        user_name = _clean_user_display_name(user.first_name if user else None)
        student_id = saved["username"] if saved else ("Configured" if is_logged_in else "Not Logged In")
        status_text = "Active & Ready ✅" if is_logged_in else "Not Logged In ❌"
        morning_text = "07:00 AM IST (Active 🔔)" if morning_on else "Disabled 🔕"

        welcome_text = (
            f"👋 <b>Welcome, {user_name}!</b>\n\n"
            f"<blockquote>🎓 <b>UniTable • KL University</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Student ID:</b> <code>{student_id}</code>\n"
            f"⚡ <b>Status:</b> <b>{status_text}</b>\n"
            f"⏰ <b>Morning Alert:</b> <b>{morning_text}</b></blockquote>\n\n"
            f"👇 <b>Select an option below to get started:</b>"
        )
        try:
            await query.edit_message_text(
                welcome_text,
                parse_mode="HTML",
                reply_markup=get_main_inline_keyboard(is_logged_in=is_logged_in, morning_enabled=morning_on),
            )
        except BadRequest:
            pass
        return

    # Refresh timetable
    if data.startswith("tt:refresh:"):
        day_arg = data.split(":", 2)[2]
        if day_arg == "week":
            await query.answer("Refreshing full week schedule... 🔄")
            msg, err = await _fetch_timetable_content(user_id, day=None, force_refresh=True)
            if err:
                try:
                    await query.edit_message_text(
                        f"❌ <b>Error:</b> {err}",
                        parse_mode="HTML",
                        reply_markup=get_account_inline_keyboard(False),
                    )
                except BadRequest:
                    pass
                return
            try:
                await query.edit_message_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=get_timetable_nav_keyboard(None),
                )
            except BadRequest as exc:
                if "Message is not modified" in str(exc):
                    await query.answer("Timetable is already up to date! ✅")
        else:
            resolved = resolve_day_name(day_arg) or _today_day_name()
            await query.answer(f"Refreshing {resolved} schedule... 🔄")
            msg, err = await _fetch_timetable_content(user_id, day=resolved, force_refresh=True)
            if err:
                try:
                    await query.edit_message_text(
                        f"❌ <b>Error:</b> {err}",
                        parse_mode="HTML",
                        reply_markup=get_account_inline_keyboard(False),
                    )
                except BadRequest:
                    pass
                return
            try:
                await query.edit_message_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=get_timetable_nav_keyboard(resolved),
                )
            except BadRequest as exc:
                if "Message is not modified" in str(exc):
                    await query.answer("Timetable is already up to date! ✅")
        return

    # Day timetable navigation (sub-second cached)
    if data.startswith("tt:day:"):
        day_arg = data.split(":", 2)[2]
        resolved = resolve_day_name(day_arg)
        if not resolved:
            await query.answer("⚠️ Invalid day", show_alert=True)
            return

        await query.answer(f"Loading {resolved}...")
        msg, err = await _fetch_timetable_content(user_id, day=resolved)

        if err:
            try:
                await query.edit_message_text(
                    f"❌ <b>Error:</b> {err}",
                    parse_mode="HTML",
                    reply_markup=get_account_inline_keyboard(False),
                )
            except BadRequest:
                pass
            return

        try:
            await query.edit_message_text(
                msg,
                parse_mode="HTML",
                reply_markup=get_timetable_nav_keyboard(resolved),
            )
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                await query.answer("Timetable is already up to date! ✅")
            else:
                logger.warning("BadRequest during timetable navigation: %s", exc)
        return

    # Full week timetable
    if data == "tt:week":
        await query.answer("Loading full week...")
        msg, err = await _fetch_timetable_content(user_id, day=None)

        if err:
            try:
                await query.edit_message_text(
                    f"❌ <b>Error:</b> {err}",
                    parse_mode="HTML",
                    reply_markup=get_account_inline_keyboard(False),
                )
            except BadRequest:
                pass
            return

        try:
            await query.edit_message_text(
                msg,
                parse_mode="HTML",
                reply_markup=get_timetable_nav_keyboard(None),
            )
        except BadRequest as exc:
            if "Message is not modified" in str(exc):
                await query.answer("Timetable is already up to date! ✅")
            else:
                logger.warning("BadRequest during week view: %s", exc)
        return

    # Day picker view
    if data == "tt:picker":
        await query.answer()
        try:
            await query.edit_message_text(
                "<blockquote>🗓️ <b>Select a Day</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Choose a weekday to view your scheduled classes:</blockquote>",
                parse_mode="HTML",
                reply_markup=get_day_selector_keyboard(),
            )
        except BadRequest:
            pass
        return

    # Account menu
    if data == "auth:menu":
        await query.answer()
        saved = credentials.load_user_credentials(user_id) if user_id else None
        is_logged_in = bool(saved or (config.ERP_USERNAME and config.ERP_PASSWORD))
        status_badge = f"<code>{saved['username']}</code> (Saved ✅)" if saved else ("Configured (.env) ✅" if is_logged_in else "Not Logged In ❌")

        account_text = (
            f"<blockquote>⚙️ <b>Account & ERP Settings</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Student ID:</b> {status_badge}</blockquote>\n\n"
            f"Select an action below:"
        )
        try:
            await query.edit_message_text(
                account_text,
                parse_mode="HTML",
                reply_markup=get_account_inline_keyboard(is_logged_in),
            )
        except BadRequest:
            pass
        return

    # Login trigger
    if data == "auth:login":
        await query.answer()
        context.user_data["login_state"] = STATE_WAITING_USERNAME
        context.user_data.pop("friend_login", None)
        await query.message.reply_text(
            "<blockquote>🔑 <b>ERP Login (Step 1 of 2)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send your ERP <b>Username / Student ID</b>:</blockquote>",
            parse_mode="HTML",
            reply_markup=get_cancel_inline_keyboard(),
        )
        return

    # Logout trigger
    if data == "auth:logout":
        await query.answer("Logged out")
        if user:
            credentials.delete_user_credentials(user.id)
            storage.delete_user_session(user.id)
            _clear_cached_timetable_html(user.id)
        try:
            await query.edit_message_text(
                "<blockquote>🚪 <b>Logged Out Successfully</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Your saved credentials and session have been cleared.</blockquote>\n\n"
                "Tap below if you wish to sign in again:",
                parse_mode="HTML",
                reply_markup=get_account_inline_keyboard(False),
            )
        except BadRequest:
            pass
        return

    # Status trigger
    if data == "auth:status":
        await query.answer()
        saved = credentials.load_user_credentials(user_id) if user_id else None
        session = load_session(user_id=user_id)
        has_session = bool(session and session.get("cookies"))
        has_saved = bool(saved or (config.ERP_USERNAME and config.ERP_PASSWORD))
        db_type = "MongoDB Atlas 🍃" if storage.is_mongodb_active() else "Local SQLite 🗄️"

        user_info = f"👤 <b>Account ID:</b> <code>{saved['username']}</code>\n" if saved else ""
        session_status = "Active & Ready ✅" if has_session else "Inactive / Stored ⏳"
        morning_status = "Active (7:00 AM IST) 🔔" if _is_morning_enabled(user_id) else "Disabled 🔕"

        status_text = (
            f"<blockquote>ℹ️ <b>UniTable • System Status</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{user_info}"
            f"🔑 <b>Credentials:</b> {'Saved & Encrypted ✅' if has_saved else 'Not Saved ❌'}\n"
            f"🌐 <b>ERP Session:</b> <b>{session_status}</b>\n"
            f"💾 <b>Database:</b> <b>{db_type}</b>\n"
            f"⏰ <b>Morning Alert:</b> <b>{morning_status}</b></blockquote>"
        )
        try:
            await query.edit_message_text(
                status_text,
                parse_mode="HTML",
                reply_markup=get_account_inline_keyboard(has_saved),
            )
        except BadRequest:
            pass
        return

    # Morning alert toggle
    if data == "morning:toggle":
        saved = credentials.load_user_credentials(user_id) if user_id else None
        if not saved and not config.ERP_USERNAME and not _is_morning_enabled(user_id):
            await query.answer("⚠️ Please log in first!", show_alert=True)
            return

        is_enabled = _is_morning_enabled(user_id)
        if is_enabled:
            _set_morning_enabled(False, user_id)
            if not _is_morning_enabled():
                if context.job_queue:
                    for job in context.job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME):
                        job.schedule_removal()
            await query.answer("🔕 Morning alert disabled")
        else:
            _set_morning_enabled(True, user_id)
            _schedule_morning_timetable_job(context)
            await query.answer("🔔 Morning alert enabled (7:00 AM IST)")

        # Refresh main menu view
        saved_u = credentials.load_user_credentials(user_id) if user_id else None
        is_logged = bool(saved_u or (config.ERP_USERNAME and config.ERP_PASSWORD))
        new_morning = _is_morning_enabled(user_id)

        user_name = _clean_user_display_name(user.first_name if user else None)
        student_id = saved_u["username"] if saved_u else ("Configured" if is_logged else "Not Logged In")
        status_text = "Active & Ready ✅" if is_logged else "Not Logged In ❌"
        morning_text = "07:00 AM IST (Active 🔔)" if new_morning else "Disabled 🔕"

        welcome_text = (
            f"👋 <b>Welcome, {user_name}!</b>\n\n"
            f"<blockquote>🎓 <b>UniTable • KL University</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Student ID:</b> <code>{student_id}</code>\n"
            f"⚡ <b>Status:</b> <b>{status_text}</b>\n"
            f"⏰ <b>Morning Alert:</b> <b>{morning_text}</b></blockquote>\n\n"
            f"👇 <b>Select an option below to get started:</b>"
        )
        try:
            await query.edit_message_text(
                welcome_text,
                parse_mode="HTML",
                reply_markup=get_main_inline_keyboard(is_logged_in=is_logged, morning_enabled=new_morning),
            )
        except BadRequest:
            pass
        return

    # Friend timetable start
    if data == "friend:start":
        await query.answer()
        context.user_data["friend_login"] = True
        context.user_data["login_state"] = STATE_WAITING_USERNAME
        await query.message.reply_text(
            "<blockquote>👥 <b>Friend's Timetable (Step 1 of 2)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Please send the ERP <b>username / ID</b> of the student whose timetable you want to view:</blockquote>",
            parse_mode="HTML",
            reply_markup=get_cancel_inline_keyboard(),
        )
        return

    # Cancel action
    if data == "cancel:action":
        context.user_data.pop("login_state", None)
        context.user_data.pop("friend_login", None)
        context.user_data.pop("erp_username", None)
        await query.answer("Cancelled")
        try:
            await query.edit_message_text(
                "❌ <b>Action cancelled.</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu:main")]]),
            )
        except BadRequest:
            pass
        return


# ---------------------------------------------------------------------------
# Text Message & Reply Keyboard Handler
# ---------------------------------------------------------------------------

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input from persistent keyboard buttons as well as conversation states."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    text = update.message.text.strip()
    state = context.user_data.get("login_state")
    is_friend_login = context.user_data.get("friend_login", False)

    # 1. Handle cancellation if user types cancel
    if text.lower() in {"cancel", "/cancel", "❌ cancel"}:
        context.user_data.pop("login_state", None)
        context.user_data.pop("friend_login", None)
        context.user_data.pop("erp_username", None)
        await update.message.reply_text("❌ Action cancelled.", reply_markup=get_main_reply_keyboard())
        return

    # 2. Handle persistent keyboard button clicks (only when not in active input state)
    if not state:
        if text in {"📅 Today's Schedule", "📅 Today's Timetable", "📅 Today"}:
            await timetable(update, context)
            return
        if text in {"📆 Full Week", "Full Week"}:
            await week(update, context)
            return
        if text in {"🗓️ Pick Day", "Pick Day"}:
            await update.message.reply_text(
                "<blockquote>🗓️ <b>Select a Day</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Choose a weekday to view your scheduled classes:</blockquote>",
                parse_mode="HTML",
                reply_markup=get_day_selector_keyboard(),
            )
            return
        if text in {"👥 Friend Schedule", "👥 Friend Timetable", "👥 Friend's Timetable"}:
            await friend(update, context)
            return
        if text in {"⏰ Morning Alert", "Morning Alert", "🔔 Morning Alert"}:
            await morning(update, context)
            return
        if text in {"⚙️ Account", "🔐 Account / Login", "🔐 Account", "Account"}:
            user = update.effective_user
            saved = credentials.load_user_credentials(user.id) if user else None
            is_logged_in = bool(saved or (config.ERP_USERNAME and config.ERP_PASSWORD))
            status_badge = f"<code>{saved['username']}</code> (Saved ✅)" if saved else ("Configured (.env) ✅" if is_logged_in else "Not Logged In ❌")
            await update.message.reply_text(
                f"<blockquote>⚙️ <b>Account & ERP Settings</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Student ID:</b> {status_badge}</blockquote>\n\n"
                f"Select an action below:",
                parse_mode="HTML",
                reply_markup=get_account_inline_keyboard(is_logged_in),
            )
            return
        if text in {"ℹ️ Status", "Status"}:
            await status(update, context)
            return
        if text in {"❓ Help", "Help"}:
            await help_command(update, context)
            return

    # 3. Handle login conversation step 1 (Username)
    if state == STATE_WAITING_USERNAME:
        context.user_data["erp_username"] = text
        context.user_data["login_state"] = STATE_WAITING_PASSWORD
        prompt_prefix = "👥 Friend's" if is_friend_login else "🔑"
        await update.message.reply_text(
            f"<blockquote>{prompt_prefix} <b>ERP Login (Step 2 of 2)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Please send the ERP password.\n"
            f"<i>(Your message will be automatically deleted for privacy)</i></blockquote>",
            parse_mode="HTML",
            reply_markup=get_cancel_inline_keyboard(),
        )
        return

    # 4. Handle login conversation step 2 (Password)
    if state == STATE_WAITING_PASSWORD:
        username = context.user_data.pop("erp_username", "")
        context.user_data.pop("login_state", None)
        context.user_data.pop("friend_login", None)
        password = text

        # Immediately delete the password message for user security
        try:
            await update.message.delete()
        except Exception:
            pass

        if not username:
            await update.message.reply_text("⚠️ An error occurred. Please start again with /login.")
            return

        if is_friend_login:
            await _run_friend_login(update, username, password)
        else:
            await _run_login(update, username, password)
        return

    # 5. Default fallback for plain text
    await update.message.reply_text(
        "💡 <b>Choose an option from the menu below or tap /start:</b>",
        parse_mode="HTML",
        reply_markup=get_main_reply_keyboard(),
    )


# ---------------------------------------------------------------------------
# Health Server & Command Registration
# ---------------------------------------------------------------------------

async def _start_health_server(app: Application) -> None:
    """Start a lightweight HTTP health responder if deployed on Render/Cloud (when PORT is set)."""
    import os
    port = os.getenv("PORT")
    if not port:
        return
    try:
        port_num = int(port)

        async def handle_ping(reader, writer):
            await reader.read(1024)
            response = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_ping, "0.0.0.0", port_num)
        logger.info("Health check server active on port %s for Render/Cloud deployment", port_num)
    except Exception as exc:
        logger.warning("Could not start cloud health check server on port %s: %s", port, exc)


async def _setup_bot_commands(app: Application) -> None:
    """Register native Telegram commands so the [/] menu button displays them cleanly."""
    commands = [
        BotCommand("start", "🏠 Open main menu & control panel"),
        BotCommand("timetable", "📅 Get today's schedule"),
        BotCommand("week", "📆 Full week timetable"),
        BotCommand("login", "🔑 Log in and save ERP credentials"),
        BotCommand("friend", "👥 View a friend's timetable"),
        BotCommand("morning", "⏰ Toggle daily 7 AM morning alert"),
        BotCommand("status", "ℹ️ Check ERP connection status"),
        BotCommand("logout", "🚪 Log out and remove credentials"),
        BotCommand("help", "❓ Help & quick guide"),
    ]
    try:
        await app.bot.set_my_commands(commands)
        logger.info("Registered %d Telegram bot commands.", len(commands))
    except Exception as exc:
        logger.warning("Could not register bot commands with Telegram: %s", exc)


async def _post_init(app: Application) -> None:
    """Post-initialization hook for cloud health check and command setup."""
    await _start_health_server(app)
    await _setup_bot_commands(app)


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

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

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("timetable", timetable))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("friend", friend))
    application.add_handler(CommandHandler("morning", morning))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # Inline Callback Query Handler
    application.add_handler(CallbackQueryHandler(callback_router))

    # Text & Reply Keyboard Message Handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Schedule the daily morning timetable job for all users with stored credentials.
    _schedule_morning_timetable_job(application)

    logger.info("UniTable Bot v%s is starting...", BOT_VERSION)
    application.run_polling()


if __name__ == "__main__":
    main()
