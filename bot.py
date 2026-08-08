import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
from erp_session import perform_manual_login, fetch_timetable_html, load_session, _session_path
from timetable_parser import format_timetable_for_telegram

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
        "👋 Hi! I am your private KL University timetable assistant.\n\n"
        "Commands:\n"
        "/login — Open the ERP login page so you can complete the CAPTCHA.\n"
        "/timetable — Fetch and send your latest timetable.\n"
        "/logout — Clear the saved ERP session.\n"
        "/status — Check whether an ERP session is saved."
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    if config.CAPTCHA_SERVICE.lower().strip() == "2captcha" and config.CAPTCHA_API_KEY:
        await update.message.reply_text(
            "🌐 Opening the ERP login page in a browser window on this computer.\n"
            "I will fill your credentials, solve the CAPTCHA automatically, and log in."
        )
    else:
        await update.message.reply_text(
            "🌐 Opening the ERP login page in a browser window on this computer.\n"
            "Please complete the CAPTCHA and log in. I will wait up to 5 minutes."
        )

    try:
        # Playwright is synchronous, so run it in a thread to avoid blocking Telegram updates.
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(None, perform_manual_login)
        cookie_count = len(session.get("cookies", []))
        csrf_status = "CSRF token captured" if session.get("csrf_token") else "CSRF token not found"
        await update.message.reply_text(
            f"✅ Login captured. {cookie_count} cookies saved. {csrf_status}.\n"
            "You can now use /timetable."
        )
    except Exception as exc:
        logger.exception("Login failed")
        await update.message.reply_text(f"❌ Login failed: {exc}")


async def timetable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    await update.message.reply_text("⏳ Fetching your timetable from the ERP...")

    try:
        html = await asyncio.get_running_loop().run_in_executor(None, fetch_timetable_html)
        message = format_timetable_for_telegram(html)
        await update.message.reply_text(message, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Failed to fetch timetable")
        await update.message.reply_text(f"❌ Failed to fetch timetable: {exc}")


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    path = _session_path()
    if os.path.exists(path):
        os.remove(path)
        await update.message.reply_text("🗑️ Saved ERP session cleared.")
    else:
        await update.message.reply_text("No saved ERP session to clear.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    session = load_session()
    if session and session.get("cookies"):
        cookie_names = ", ".join(c.get("name", "?") for c in session["cookies"])
        await update.message.reply_text(
            f"💾 Session saved. Cookies: {cookie_names}\n"
            f"CSRF token: {'yes' if session.get('csrf_token') else 'no'}"
        )
    else:
        await update.message.reply_text("No saved ERP session. Use /login first.")


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

    logger.info("Bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
