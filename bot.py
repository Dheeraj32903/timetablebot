import asyncio
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import (
    Application,
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

# Daily automatic timetable message job name.
MORNING_TIMETABLE_JOB_NAME = "morning_timetable"


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        if update.message:
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    await update.message.reply_text(
        "🎓 <b>KL University Timetable Bot</b>\n\n"
        "<b>Available Commands:</b>\n"
        "/login — Log in and save your ERP credentials\n"
        "/timetable — Get today's timetable\n"
        "/timetable &lt;day&gt; — Get timetable for a day (today/yesterday/tomorrow/mon/...)\n"
        "/week — Get full week timetable\n"
        "/friend — Get another user's timetable without saving credentials\n"
        "/morning — Toggle daily morning timetable alert\n"
        "/logout — Log out and remove saved credentials\n"
        "/status — Check database and login status",
        parse_mode="HTML"
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
            "Your credentials are saved securely. You won't need to log in again.\n"
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
    if user:
        credentials.delete_user_credentials(user.id)
        storage.delete_user_session(user.id)

    await update.message.reply_text("🚪 Logged out and removed your saved credentials from the database.")


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

            message = format_timetable_for_telegram(html, day=today_name)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🌅 <b>Good morning! Here is today's schedule ({today_name}):</b>\n\n{message}",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to send morning timetable to chat_id=%s", chat_id)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Could not fetch your morning timetable. Try /login and then /timetable.",
                )
            except Exception:
                pass


def _schedule_morning_timetable_job(context) -> None:
    """Schedule the daily morning timetable job if enabled and not already scheduled."""
    if not _is_morning_enabled():
        return

    job_queue = getattr(context, "job_queue", None)
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
        await update.message.reply_text("🔕 Morning timetable message disabled.")
    else:
        _set_morning_enabled(True, user.id)
        _schedule_morning_timetable_job(context)
        await update.message.reply_text(
            f"🔔 Morning timetable message enabled. You will receive it daily at "
            f"<b>{config.MORNING_TIMETABLE_HOUR:02d}:{config.MORNING_TIMETABLE_MINUTE:02d} IST</b>.",
            parse_mode="HTML"
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    session = load_session(user_id=uid)
    db_type = "MongoDB Atlas 🍃" if storage.is_mongodb_active() else "Local SQLite 🗄️"
    
    if session and session.get("cookies"):
        await update.message.reply_text(f"✅ You are logged in.\nDatabase backend: <b>{db_type}</b>", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ You are logged out. Use /login first.\nDatabase backend: <b>{db_type}</b>", parse_mode="HTML")


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


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(protect_content=True))
        .job_queue(JobQueue())
        .post_init(_start_health_server)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("timetable", timetable))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("friend", friend))
    application.add_handler(CommandHandler("morning", morning))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_message))

    # Schedule the daily morning timetable job for all users with stored credentials.
    _schedule_morning_timetable_job(application)

    logger.info("KL University Timetable Bot v%s is starting...", BOT_VERSION)
    application.run_polling()


if __name__ == "__main__":
    main()
