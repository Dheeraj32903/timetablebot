import asyncio
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path

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
from erp.session import perform_login, fetch_timetable_html, load_session, SESSION_PATH
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

# Auto-logout session 5 minutes after fetching the timetable.
SESSION_TIMEOUT_SECONDS = 300

# Daily automatic timetable message job name.
MORNING_TIMETABLE_JOB_NAME = "morning_timetable"


def _is_morning_enabled() -> bool:
    """Return True if the daily morning timetable is enabled."""
    if not config.MORNING_ENABLED_FILE.exists():
        return False
    return config.MORNING_ENABLED_FILE.read_text(encoding="utf-8").strip() == "1"


def _set_morning_enabled(enabled: bool) -> None:
    config.MORNING_ENABLED_FILE.write_text("1" if enabled else "0", encoding="utf-8")


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not config.AUTHORIZED_TELEGRAM_USER_IDS:
        return True
    return user.id in config.AUTHORIZED_TELEGRAM_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    await update.message.reply_text(
        "KL University Timetable Bot\n\n"
        "Commands:\n"
        "/login — Log in and save your ERP credentials\n"
        "/timetable — Get today's timetable\n"
        "/timetable <day> — Get timetable for a day (today/yesterday/tomorrow/mon/...)\n"
        "/week — Get full week timetable\n"
        "/friend — Get another user's timetable\n"
        "/morning — Toggle daily 7 AM timetable message\n"
        "/logout — Log out and remove saved credentials\n"
        "/status — Check login status"
    )


async def _run_login(update: Update, username: str, password: str) -> None:
    """Run the async ERP login and report the result."""
    await update.message.reply_text("Logging in...")
    try:
        await perform_login(username, password)

        # Save credentials for this Telegram user for future automatic use.
        if update.effective_user:
            credentials.save_user_credentials(
                update.effective_user.id, username, password
            )

        await update.message.reply_text(
            "Logged in successfully.\n"
            "Your credentials are saved securely. You won't need to log in again.\n"
            "Send /timetable to get your timetable."
        )
    except Exception as exc:
        logger.exception("Login failed")
        await update.message.reply_text(
            "Login failed. Please check your credentials and try again."
        )


async def _run_friend_login(update: Update, username: str, password: str) -> None:
    """Log in with another user's credentials and send their timetable without saving them."""
    await update.message.reply_text("Logging in and fetching timetable...")
    try:
        await perform_login(username, password)
        html = await fetch_timetable_html()
        message = format_timetable_for_telegram(html, day=_today_day_name())

        await update.message.reply_text(
            message,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Friend login failed")
        await update.message.reply_text(
            "Could not fetch timetable. Please check the credentials and try again."
        )
    finally:
        # Always clear the session so the next user is not logged in as the friend.
        if SESSION_PATH.exists():
            SESSION_PATH.unlink()


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
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
    await update.message.reply_text("Send your ERP username.")


async def handle_login_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle username/password replies during the /login or /friend conversation."""
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    state = context.user_data.get("login_state")
    text = update.message.text.strip()
    is_friend_login = context.user_data.get("friend_login", False)

    if state == STATE_WAITING_USERNAME:
        context.user_data["erp_username"] = text
        context.user_data["login_state"] = STATE_WAITING_PASSWORD
        await update.message.reply_text("Send your ERP password.")
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
            await update.message.reply_text("Something went wrong. Start again.")
            return

        if is_friend_login:
            await _run_friend_login(update, username, password)
        else:
            await _run_login(update, username, password)
        return

    # If we are not in a login conversation, ignore plain text.
    await update.message.reply_text(
        "Use /login to log in, /timetable for today's timetable, /week for the full week, or /friend for another user's timetable."
    )


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    # Parse optional day argument, e.g. /timetable tomorrow or /timetable mon.
    arg = " ".join(context.args).strip() if context.args else ""
    day = resolve_day_name(arg)
    if day is None:
        await update.message.reply_text(
            "Invalid day. Use: today, yesterday, tomorrow, mon, tue, wed, thu, fri, sat, sun."
        )
        return

    await update.message.reply_text("Fetching your timetable...")

    try:
        html = await fetch_timetable_html()
        message = format_timetable_for_telegram(html, day=day)

        # Schedule automatic session cleanup after 5 minutes.
        if context.job_queue:
            context.job_queue.run_once(
                auto_logout_job,
                when=SESSION_TIMEOUT_SECONDS,
                chat_id=update.effective_chat.id,
                name="auto_logout",
            )

        await update.message.reply_text(
            message,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Failed to fetch timetable")

        # Try to auto-login using stored credentials and retry once.
        user = update.effective_user
        saved = credentials.load_user_credentials(user.id) if user else None
        if saved:
            try:
                await update.message.reply_text("Logging you in automatically...")
                await perform_login(saved["username"], saved["password"])
                html = await fetch_timetable_html()
                message = format_timetable_for_telegram(html, day=day)

                if context.job_queue:
                    context.job_queue.run_once(
                        auto_logout_job,
                        when=SESSION_TIMEOUT_SECONDS,
                        chat_id=update.effective_chat.id,
                        name="auto_logout",
                    )

                await update.message.reply_text(
                    message,
                    parse_mode="HTML",
                )
                return
            except Exception:
                logger.exception("Auto-login retry failed")

        await update.message.reply_text(
            "Could not fetch timetable.\n"
            "Try logging in again with /login."
        )


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    await update.message.reply_text("Fetching your full week timetable...")

    try:
        html = await fetch_timetable_html()
        message = format_timetable_for_telegram(html)

        if context.job_queue:
            context.job_queue.run_once(
                auto_logout_job,
                when=SESSION_TIMEOUT_SECONDS,
                chat_id=update.effective_chat.id,
                name="auto_logout",
            )

        await update.message.reply_text(
            message,
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("Failed to fetch timetable")

        user = update.effective_user
        saved = credentials.load_user_credentials(user.id) if user else None
        if saved:
            try:
                await update.message.reply_text("Logging you in automatically...")
                await perform_login(saved["username"], saved["password"])
                html = await fetch_timetable_html()
                message = format_timetable_for_telegram(html)

                if context.job_queue:
                    context.job_queue.run_once(
                        auto_logout_job,
                        when=SESSION_TIMEOUT_SECONDS,
                        chat_id=update.effective_chat.id,
                        name="auto_logout",
                    )

                await update.message.reply_text(
                    message,
                    parse_mode="HTML",
                )
                return
            except Exception:
                logger.exception("Auto-login retry failed")

        await update.message.reply_text(
            "Could not fetch timetable.\n"
            "Try logging in again with /login."
        )


async def friend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch another user's timetable by entering their ERP credentials."""
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return


    context.user_data["friend_login"] = True
    context.user_data["login_state"] = STATE_WAITING_USERNAME
    await update.message.reply_text(
        "Send the ERP username of the person whose timetable you want to see."
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    if SESSION_PATH.exists():
        SESSION_PATH.unlink()

    user = update.effective_user
    if user:
        credentials.delete_user_credentials(user.id)

    await update.message.reply_text("Logged out and removed your saved credentials.")


async def auto_logout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: clear the ERP session after a timeout."""
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()


async def _send_morning_timetable(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: send today's timetable to every user with stored credentials."""
    all_creds = credentials.all_user_credentials()
    if not all_creds:
        logger.warning("Morning timetable skipped: no stored credentials.")
        return

    for user_id_str, creds in all_creds.items():
        chat_id = int(user_id_str)
        try:
            await perform_login(creds["username"], creds["password"])
            html = await fetch_timetable_html()
            message = format_timetable_for_telegram(html, day=_today_day_name())

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🌅 <b>Good morning! Here is today's schedule:</b>\n\n{message}",
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
                logger.exception("Could not send error message to chat_id=%s", chat_id)
        finally:
            if SESSION_PATH.exists():
                SESSION_PATH.unlink()


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
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    if not credentials.all_user_credentials():
        await update.message.reply_text(
            "No stored credentials found. Please log in with /login first."
        )
        return

    current_jobs = context.job_queue.get_jobs_by_name(MORNING_TIMETABLE_JOB_NAME)
    if current_jobs:
        for job in current_jobs:
            job.schedule_removal()
        _set_morning_enabled(False)
        await update.message.reply_text("Morning timetable message disabled.")
    else:
        _set_morning_enabled(True)
        _schedule_morning_timetable_job(context)
        await update.message.reply_text(
            f"Morning timetable message enabled. You will receive it daily at "
            f"{config.MORNING_TIMETABLE_HOUR:02d}:{config.MORNING_TIMETABLE_MINUTE:02d} IST."
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return


    session = load_session()
    if session and session.get("cookies"):
        await update.message.reply_text("You are logged in.")
    else:
        await update.message.reply_text("You are logged out. Use /login first.")


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

    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
