import asyncio
import logging
import time
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from erp.session import perform_login, fetch_timetable_html, load_session, SESSION_PATH
from parser.timetable import format_timetable_for_telegram

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


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id == config.AUTHORIZED_TELEGRAM_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    await update.message.reply_text(
        "KL University Timetable Bot\n\n"
        "Commands:\n"
        "/login — Log in to the ERP\n"
        "/timetable — Get your timetable\n"
        "/logout — Log out\n"
        "/status — Check login status"
    )


async def _run_login(update: Update, username: str, password: str) -> None:
    """Run the async ERP login and report the result."""
    await update.message.reply_text("Logging in...")
    start = time.perf_counter()
    try:
        await perform_login(username, password)
        elapsed = time.perf_counter() - start
        await update.message.reply_text(
            f"Logged in successfully. ({elapsed:.2f}s)\n"
            "Send /timetable to get your timetable."
        )
    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.exception("Login failed")
        await update.message.reply_text(
            f"Login failed. ({elapsed:.2f}s)\n"
            "Please check your credentials and try again."
        )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    # If credentials are configured in .env, use them directly.
    if config.ERP_USERNAME and config.ERP_PASSWORD:
        await _run_login(update, config.ERP_USERNAME, config.ERP_PASSWORD)
        return

    # Otherwise, start a conversation to collect credentials in Telegram chat.
    context.user_data["login_state"] = STATE_WAITING_USERNAME
    await update.message.reply_text("Send your ERP username.")


async def handle_login_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle username/password replies during the /login conversation."""
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    state = context.user_data.get("login_state")
    text = update.message.text.strip()

    if state == STATE_WAITING_USERNAME:
        context.user_data["erp_username"] = text
        context.user_data["login_state"] = STATE_WAITING_PASSWORD
        await update.message.reply_text("Send your ERP password.")
        return

    if state == STATE_WAITING_PASSWORD:
        username = context.user_data.pop("erp_username", "")
        context.user_data.pop("login_state", None)
        password = text

        if not username:
            await update.message.reply_text("Something went wrong. Start again with /login.")
            return

        await _run_login(update, username, password)
        return

    # If we are not in a login conversation, ignore plain text.
    await update.message.reply_text("Use /login to log in, or /timetable to get your timetable.")


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    await update.message.reply_text("Fetching your timetable...")
    start = time.perf_counter()

    try:
        html = await fetch_timetable_html()
        message = format_timetable_for_telegram(html)
        elapsed = time.perf_counter() - start

        # Schedule automatic session cleanup after 5 minutes.
        context.job_queue.run_once(
            auto_logout_job,
            when=SESSION_TIMEOUT_SECONDS,
            chat_id=update.effective_chat.id,
            name="auto_logout",
        )

        await update.message.reply_text(
            f"{message}\n\n_Fetched in {elapsed:.2f}s_",
            parse_mode="Markdown",
        )
    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.exception("Failed to fetch timetable")
        await update.message.reply_text(
            f"Could not fetch timetable. ({elapsed:.2f}s)\n"
            "Try logging in again with /login."
        )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
        await update.message.reply_text("Logged out.")
    else:
        await update.message.reply_text("You are already logged out.")


async def auto_logout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: clear the ERP session after a timeout."""
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    session = load_session()
    if session and session.get("cookies"):
        await update.message.reply_text("You are logged in.")
    else:
        await update.message.reply_text("You are logged out. Use /login first.")


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    if config.AUTHORIZED_TELEGRAM_USER_ID == 0:
        raise RuntimeError("AUTHORIZED_TELEGRAM_USER_ID is not set. Check your .env file.")

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("timetable", timetable))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_message))

    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
