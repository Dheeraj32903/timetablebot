import asyncio
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional, Tuple

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states for collecting credentials in Telegram chat.
STATE_WAITING_USERNAME = "waiting_username"
STATE_WAITING_PASSWORD = "waiting_password"

BOT_VERSION = "5.2.0"

# Daily automatic timetable message job names.
MORNING_TIMETABLE_JOB_NAME = "morning_timetable"
MORNING_PREFETCH_JOB_NAME = "morning_prefetch"
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
    BotCommand("login", "Connect & save ERP credentials"),
    BotCommand("morning", "Sends the day's schedule daily at 7 AM"),
    BotCommand("status", "Check ERP connection status"),
    BotCommand("description", "Why use this bot & key benefits"),
    BotCommand("privacy", "View bot privacy policy"),
    BotCommand("logout", "Log out & clear your data"),
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
        "🎓 <b>Your Timetable Assistant welcomes you!</b>\n\n"
        "Your classes, schedule &amp; rooms — all in one place.\n\n"
        "✨ <b>What you can do:</b>\n\n"
        "📅 Check today's classes &amp; room numbers\n\n"
        "🗓️ View your full weekly timetable\n\n"
        "⏰ Get daily morning schedule alerts\n\n"
        "👥 Check your friends' schedules\n\n"
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
    """Handle username/password replies during the /login or /friend conversation."""
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
        await update.message.reply_text("❌ Action cancelled.")
        return

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

    # If we are not in a login conversation, ignore plain text.
    await update.message.reply_text(
        "💡 Use /login to log in, /timetable for today's timetable, /week for the full week, or /friend for another user's timetable."
    )


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
            await status_msg.edit_text("⚠️ <i>Could not extract timetable.</i>", parse_mode="HTML")
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
            "❌ Could not fetch timetable.\n"
            "Try logging in again with /login."
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
            await status_msg.edit_text("⚠️ <i>Could not extract timetable.</i>", parse_mode="HTML")
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
            "❌ Could not fetch timetable.\n"
            "Try logging in again with /login."
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
    """Background warmup job (starts 10 mins before 07:00 IST at 06:50 IST): pre-authenticates and pre-fetches timetables."""
    morning_uids = storage.get_all_morning_users()
    if not morning_uids:
        return

    logger.info("Starting morning pre-fetch warmup for %d users...", len(morning_uids))
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
                await perform_login(saved["username"], saved["password"], user_id=chat_id)
                html = await fetch_timetable_html(user_id=chat_id)

            _morning_cache[chat_id] = html
            logger.info("Pre-fetch successful for user %s", chat_id)
        except Exception:
            logger.warning("Pre-fetch initial attempt failed for user %s, queued for retry", chat_id)
            failed_users.append((chat_id, saved))
        finally:
            await asyncio.sleep(1.0)

    # Second retry pass for any failed users before 07:00 IST
    if failed_users:
        logger.info("Retrying pre-fetch for %d users...", len(failed_users))
        await asyncio.sleep(3.0)
        for chat_id, saved in failed_users:
            try:
                await perform_login(saved["username"], saved["password"], user_id=chat_id)
                html = await fetch_timetable_html(user_id=chat_id)
                _morning_cache[chat_id] = html
                logger.info("Pre-fetch retry successful for user %s", chat_id)
            except Exception:
                logger.exception("Pre-fetch retry failed for user %s", chat_id)
            finally:
                await asyncio.sleep(1.0)


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
            # Use pre-fetched HTML from 06:50 warmup if available
            html = _morning_cache.get(chat_id)
            if not html:
                try:
                    html = await fetch_timetable_html(user_id=chat_id)
                except Exception:
                    await perform_login(saved["username"], saved["password"], user_id=chat_id)
                    html = await fetch_timetable_html(user_id=chat_id)

            message = format_timetable_for_telegram(html, day=today_name)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🌅 <b>Good morning! Here is today's schedule ({today_name}):</b>\n\n{message}",
                parse_mode="HTML",
            )
        except Exception:
            # Resilient fallback retry pass with fresh login before failing
            try:
                logger.warning("Morning delivery first attempt failed for chat_id=%s, attempting re-login...", chat_id)
                await asyncio.sleep(1.0)
                await perform_login(saved["username"], saved["password"], user_id=chat_id)
                html = await fetch_timetable_html(user_id=chat_id)
                message = format_timetable_for_telegram(html, day=today_name)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🌅 <b>Good morning! Here is today's schedule ({today_name}):</b>\n\n{message}",
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Failed to send morning timetable to chat_id=%s after retries", chat_id)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="Could not fetch your morning timetable. Try /login and then /timetable.",
                    )
                except Exception:
                    pass
        finally:
            await asyncio.sleep(0.5)

    # Clear cache for the next day
    _morning_cache.clear()


def _schedule_morning_timetable_job(context) -> None:
    """Schedule the daily morning timetable pre-fetch (06:50 IST) and delivery (07:00 IST) jobs."""
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

    # Delivery time (e.g., 07:00 IST)
    scheduled_time = dt_time(
        hour=config.MORNING_TIMETABLE_HOUR,
        minute=config.MORNING_TIMETABLE_MINUTE,
        tzinfo=tz,
    )

    # Pre-fetch warmup time (10 minutes earlier, e.g., 06:50 IST)
    prefetch_hour = config.MORNING_TIMETABLE_HOUR
    prefetch_minute = config.MORNING_TIMETABLE_MINUTE - 10
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
        "🔒 <b>PRIVACY POLICY</b>\n\n"
        "<b>1. Data We Store</b>\n"
        "• <b>Telegram User ID:</b> Used as your unique account ID to link your stored preferences and credentials.\n"
        "• <b>ERP Credentials:</b> When you use <code>/login</code>, your student username and password are encrypted with <b>Fernet (AES-128-CBC + HMAC)</b> before saving. Plaintext passwords are never stored, and password messages in chat are deleted immediately.\n"
        "• <b>ERP Session Data:</b> Active session cookies and CSRF tokens are stored to maintain your authenticated session.\n"
        "• <b>Alert Preferences:</b> Your daily morning schedule alert setting (enabled/disabled).\n\n"
        "<b>2. Timetable & Live Processing</b>\n"
        "• <b>Timetable Data:</b> Fetched on-demand live from the university ERP portal. Schedules are parsed in memory and sent directly to you; we <i>do not</i> permanently store your timetable or academic data.\n"
        "• <b>Friend Feature (<code>/friend</code>):</b> 100% ephemeral. Credentials entered for a friend are processed in memory for a single fetch and are <i>never</i> saved to the database.\n\n"
        "<b>3. Hosting & Infrastructure</b>\n"
        "• <b>MongoDB Atlas:</b> Encrypted user credentials, session cookies, and settings are stored in a secured MongoDB Atlas cloud database.\n"
        "• <b>Render:</b> The bot application runs as a secure cloud service on Render.\n"
        "• <b>Telegram:</b> Messages and commands are transmitted via the official Telegram Bot API.\n\n"
        "<b>4. Your Control & Data Deletion</b>\n"
        "• <b>/logout:</b> Clears your saved ERP credentials, active sessions, and data from the database."
    )

    await update.message.reply_text(privacy_text, parse_mode="HTML")


async def description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send project description and key benefits of using the bot."""
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    desc_text = (
        "💡 <b>Why use this bot?</b>\n\n"
        "• 📅 <b>Direct ERP Sync:</b>\n"
        "Get your latest timetable directly from the ERP.\n\n"
        "• ⚡ <b>Offline Access:</b>\n"
        "In case of an emergency or when you don't have internet, you can simply open the timetable message already received in Telegram instead of going back to the ERP and logging in again.\n\n"
        "• 🔄 <b>Live Updates:</b>\n"
        "If you know there is a change in timetable, simply send the <code>/timetable</code> command again."
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
    """Register active Telegram commands."""
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
    application.add_handler(CommandHandler("morning", morning))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("description", description))
    application.add_handler(CommandHandler("describe", description))
    application.add_handler(CommandHandler("privacy", privacy))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(ChatMemberHandler(handle_user_status_change, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_message))

    # Schedule the daily morning timetable job for all users with stored credentials.
    _schedule_morning_timetable_job(application)

    logger.info("KL University Timetable Bot v%s is starting...", BOT_VERSION)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
