import logging
from datetime import datetime, date, timedelta
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import config
import credentials
import erp.outpass as outpass_api
from calendar_ui import create_calendar, parse_calendar_callback
from erp.session import load_session
from security import user_concurrency

logger = logging.getLogger(__name__)

STATE_WAITING_OUTPASS_REASON = "waiting_outpass_reason"

# In-memory store for testing outpass features when ERP account is not connected
_test_outpass_store: dict = {}


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not config.AUTHORIZED_TELEGRAM_USER_IDS:
        return True
    return user.id in config.AUTHORIZED_TELEGRAM_USER_IDS


def _format_time_friendly(dt_str: str) -> str:
    """Convert '2026-07-11 16:00:31' or '16:00:00' to friendly AM/PM display."""
    if not dt_str:
        return "N/A"
    try:
        if " " in dt_str:
            d_part, t_part = dt_str.split(" ", 1)
            t_obj = datetime.strptime(t_part[:5], "%H:%M")
            return f"{d_part} {t_obj.strftime('%I:%M %p')}"
        elif ":" in dt_str:
            t_obj = datetime.strptime(dt_str[:5], "%H:%M")
            return t_obj.strftime("%I:%M %p")
    except Exception:
        pass
    return dt_str


def _friendly_date(date_str: str) -> str:
    """Convert '2026-09-12' to '12 September 2026' matching ERP format."""
    if not date_str:
        return ""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return d.strftime("%d %B %Y")
    except Exception:
        return date_str


def _status_badge(status: str) -> str:
    st = str(status).strip().lower()
    if any(w in st for w in ["approv", "accept", "permit", "allow", "issued", "pass"]):
        return "🟢 <b>Approved</b>"
    if any(w in st for w in ["pend", "wait"]):
        return "⏳ <b>Pending Warden Approval</b>"
    if any(w in st for w in ["reject", "declin", "deni"]):
        return "🔴 <b>Rejected</b>"
    if any(w in st for w in ["cancel"]):
        return "⚪ <b>Cancelled</b>"
    return f"<b>{status}</b>"


def create_hour_picker(step_prefix: str, selected_date: str) -> InlineKeyboardMarkup:
    """
    Generate 24-hour picker matching ERP DateTimePicker (4 columns x 6 rows).
    step_prefix: 'home_out', 'home_in', 'loc_out', 'loc_in'
    """
    keyboard = []
    for row_start in range(0, 24, 4):
        row = []
        for h in range(row_start, row_start + 4):
            row.append(
                InlineKeyboardButton(
                    f"{h}:00",
                    callback_data=f"outpass:hr:{step_prefix}:{h}"
                )
            )
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel")])
    return InlineKeyboardMarkup(keyboard)


def create_minute_picker(step_prefix: str, hour: int, selected_date: str) -> InlineKeyboardMarkup:
    """
    Generate 5-minute picker matching ERP DateTimePicker (4 columns x 3 rows).
    5-minute increments: 00, 05, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55
    """
    keyboard = []
    minutes = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
    for row_start in range(0, 12, 4):
        row = []
        for m in minutes[row_start:row_start + 4]:
            label = f"{hour}:{m:02d}"
            time_val = f"{hour:02d}:{m:02d}:00"
            row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"outpass:min:{step_prefix}:{time_val}"
                )
            )
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton("🔙 Back to Hours", callback_data=f"outpass:back_hr:{step_prefix}"),
        InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel"),
    ])
    return InlineKeyboardMarkup(keyboard)


def format_outpass_history_message(outpasses: list) -> str:
    if not outpasses:
        return (
            "🏠 <b>Hostel Outpass Status</b>\n\n"
            "ℹ️ No active outpass requests found.\n\n"
            "Tap <b>➕ Apply New Outpass</b> below to test or submit an e-outing request!"
        )

    # Focus strictly on the latest / active outpass status
    latest = outpasses[0]
    badge = _status_badge(latest.get("status", ""))
    out_t = _format_time_friendly(latest.get("out_time", ""))
    in_t = _format_time_friendly(latest.get("in_time", ""))

    msg = [
        "🏠 <b>Hostel Outpass Status</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"🎫 <b>Request #{latest.get('id', 'N/A')}</b>",
        f"📅 <b>Outing Date:</b> <code>{_friendly_date(latest.get('outing_date', 'N/A'))}</code>",
        f"📌 <b>Status:</b> {badge}",
        f"🛫 <b>Out Date & Time:</b> <code>{out_t}</code>",
        f"🛬 <b>In Date & Time:</b> <code>{in_t}</code>",
        f"📝 <b>Reason:</b> <i>{latest.get('reason', 'N/A')}</i>",
        f"👮 <b>Warden:</b> {latest.get('warden_name', 'N/A')}",
    ]

    if latest.get("approved_by") and latest.get("approved_by") != "N/A":
        msg.append(f"✍️ <b>Approved By:</b> <code>{latest.get('approved_by')}</code>")

    return "\n".join(msg)


async def outpass_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View current active hostel outpass status."""
    if not is_authorized(update):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ You are not authorized to use this bot.")
        return

    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    async with user_concurrency.task_guard(uid) as acquired:
        if not acquired:
            await update.message.reply_text(
                "⏳ <b>Request in progress.</b>\nYour previous request is still running. Please wait for it to finish.",
                parse_mode="HTML",
            )
            return

        status_msg = await update.message.reply_text("⏳ <b>Checking your outpass status...</b>", parse_mode="HTML")

        try:
            outpasses = []
            saved = credentials.load_user_credentials(uid)
            sess = load_session(user_id=uid)
            if saved and sess and sess.get("cookies"):
                try:
                    outpasses = await outpass_api.fetch_outpass_history(user_id=uid)
                except Exception:
                    outpasses = []

            if not outpasses:
                outpasses = _test_outpass_store.get(uid, [])

            text = format_outpass_history_message(outpasses)

            buttons = [
                [
                    InlineKeyboardButton("🔄 Refresh Status", callback_data="outpass:refresh"),
                    InlineKeyboardButton("➕ Apply New Outpass", callback_data="outpass:start"),
                ]
            ]
            if uid in _test_outpass_store:
                buttons.append([
                    InlineKeyboardButton("🧪 Toggle Status (Approved ⇄ Pending)", callback_data="outpass:toggle_test_status")
                ])
                buttons.append([
                    InlineKeyboardButton("🗑️ Clear / Reset Test Pass", callback_data="outpass:clear_test_status")
                ])
            keyboard = InlineKeyboardMarkup(buttons)
            await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as exc:
            logger.error("Outpass command error: %s", exc)
            err_text = (
                "❌ <b>Could not load outpass status.</b>\n\n"
                f"<i>{exc}</i>\n\n"
                "Make sure you are logged in with /login or try again."
            )
            await status_msg.edit_text(err_text, parse_mode="HTML")


async def start_outpass_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the interactive outpass application wizard."""
    if not is_authorized(update):
        return

    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    context.user_data["outpass_flow"] = {}
    context.user_data.pop("login_state", None)

    saved = credentials.load_user_credentials(uid)
    sess = load_session(user_id=uid)
    is_live = bool(saved and sess and sess.get("cookies"))
    mode_hint = "" if is_live else "\n<i>(🧪 Test / Demo Mode active — connect /login for live ERP)</i>\n"

    text = (
        "🏠 <b>Hostel Outpass Application</b>\n\n"
        f"{mode_hint}"
        "Please select your outpass category:\n\n"
        "• ⏱️ <b>Local Outing (Hours)</b>: Return on the same day within regular campus hours.\n"
        "• 🏡 <b>Home Outing (Days)</b>: Weekend trip or travel home."
    )

    buttons = [
        [
            InlineKeyboardButton("⏱️ Local Outing (Hours)", callback_data="outpass:type:hours"),
            InlineKeyboardButton("🏡 Home Outing (Days)", callback_data="outpass:type:days"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="outpass:cancel")
        ]
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# Backward compatibility alias
apply_outpass_command = start_outpass_wizard


async def handle_outpass_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Central router for outpass wizard interactions and calendar date picking."""
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        return

    data = query.data
    user = update.effective_user
    uid = user.id if user else None
    if not uid:
        return

    # --- Cancel action ---
    if data in ("outpass:cancel", "outcal_local:cancel", "outcal_home_out:cancel", "outcal_home_in:cancel"):
        context.user_data.pop("outpass_flow", None)
        context.user_data.pop("login_state", None)
        await query.edit_message_text("❌ <b>Outpass application cancelled.</b>", parse_mode="HTML")
        return

    # --- Start wizard ---
    if data == "outpass:start":
        await start_outpass_wizard(update, context)
        return

    # --- Refresh History ---
    if data == "outpass:refresh":
        async with user_concurrency.task_guard(uid) as acquired:
            if not acquired:
                await query.answer("⏳ Outpass refresh already in progress...", show_alert=False)
                return

            await query.edit_message_text("🔄 <i>Refreshing outpass status...</i>", parse_mode="HTML")
            try:
                outpasses = []
                saved = credentials.load_user_credentials(uid)
                sess = load_session(user_id=uid)
                if saved and sess and sess.get("cookies"):
                    try:
                        outpasses = await outpass_api.fetch_outpass_history(user_id=uid)
                    except Exception:
                        outpasses = []

                if not outpasses:
                    outpasses = _test_outpass_store.get(uid, [])

                text = format_outpass_history_message(outpasses)
                buttons = [
                    [
                        InlineKeyboardButton("🔄 Refresh Status", callback_data="outpass:refresh"),
                        InlineKeyboardButton("➕ Apply New Outpass", callback_data="outpass:start"),
                    ]
                ]
                if uid in _test_outpass_store:
                    buttons.append([
                        InlineKeyboardButton("🧪 Toggle Status (Approved ⇄ Pending)", callback_data="outpass:toggle_test_status")
                    ])
                    buttons.append([
                        InlineKeyboardButton("🗑️ Clear / Reset Test Pass", callback_data="outpass:clear_test_status")
                    ])
                keyboard = InlineKeyboardMarkup(buttons)
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as exc:
                logger.error("Outpass refresh error: %s", exc)
                await query.edit_message_text("❌ <i>Could not refresh outpass status. Please try again.</i>", parse_mode="HTML")
            return

    # --- Toggle Test Outpass Status ---
    if data == "outpass:toggle_test_status":
        records = _test_outpass_store.get(uid, [])
        if records:
            cur = records[0].get("status", "Pending")
            if cur == "Approved":
                records[0]["status"] = "Pending"
                records[0]["approved_by"] = "Pending Approval"
            else:
                records[0]["status"] = "Approved"
                records[0]["approved_by"] = "Dr. K. Srinivas (Chief Warden)"
        text = format_outpass_history_message(records)
        buttons = [
            [
                InlineKeyboardButton("🔄 Refresh Status", callback_data="outpass:refresh"),
                InlineKeyboardButton("➕ Apply New Outpass", callback_data="outpass:start"),
            ],
            [
                InlineKeyboardButton("🧪 Toggle Status (Approved ⇄ Pending)", callback_data="outpass:toggle_test_status")
            ],
            [
                InlineKeyboardButton("🗑️ Clear / Reset Test Pass", callback_data="outpass:clear_test_status")
            ]
        ]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # --- Clear / Reset Test Outpass ---
    if data == "outpass:clear_test_status":
        _test_outpass_store.pop(uid, None)
        text = format_outpass_history_message([])
        buttons = [
            [
                InlineKeyboardButton("🔄 Refresh Status", callback_data="outpass:refresh"),
                InlineKeyboardButton("➕ Apply New Outpass", callback_data="outpass:start"),
            ]
        ]
        await query.edit_message_text(
            "🗑️ <b>Test outpass reset successfully!</b>\n\n" + text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Ensure flow state exists
    flow = context.user_data.setdefault("outpass_flow", {})

    # ==========================================
    # 1. TYPE SELECTION
    # ==========================================
    if data == "outpass:type:hours":
        flow.clear()
        flow["type"] = "hours"
        flow["meals"] = {"breakfast": "NO", "lunch": "NO", "snack": "NO", "dinner": "NO"}
        now = datetime.now()
        cal_kb = create_calendar(now.year, now.month, prefix="outcal_local", min_date=now.date())
        await query.edit_message_text(
            "⏱️ <b>Local Outing — Step 1 of 5</b>\n\n"
            "📅 <b>Select Outpass Date:</b>\n"
            "Tap your outing date on the calendar below:",
            parse_mode="HTML",
            reply_markup=cal_kb,
        )
        return

    if data == "outpass:type:days":
        flow.clear()
        flow["type"] = "days"
        now = datetime.now()
        cal_kb = create_calendar(now.year, now.month, prefix="outcal_home_out", min_date=now.date())
        await query.edit_message_text(
            "🏡 <b>Home Outing — Step 1 of 5</b>\n\n"
            "📅 <b>Select Departure Date:</b>\n"
            "Tap your departure date on the calendar below:",
            parse_mode="HTML",
            reply_markup=cal_kb,
        )
        return

    # ==========================================
    # 2. LOCAL OUTING CALENDAR & HOURS
    # ==========================================
    if data.startswith("outcal_local:"):
        action, payload = parse_calendar_callback(data)
        if action == "ignore":
            return
        if action == "nav":
            y, m = map(int, payload.split(":"))
            new_kb = create_calendar(y, m, prefix="outcal_local", min_date=datetime.now().date())
            await query.edit_message_reply_markup(reply_markup=new_kb)
            return
        if action == "sel":
            flow["date"] = payload  # YYYY-MM-DD
            await query.edit_message_text(
                f"⏱️ <b>Local Outing — Out Date & Time</b>\n\n"
                f"📅 Date: <b>{_friendly_date(payload)}</b>\n\n"
                f"⏰ <b>Select Departure Hour:</b>\n"
                f"Tap your departure hour below (0:00 — 23:00):",
                parse_mode="HTML",
                reply_markup=create_hour_picker("loc_out", payload),
            )
            return

    # ==========================================
    # 3. HOME OUTING CALENDARS
    # ==========================================
    if data.startswith("outcal_home_out:"):
        action, payload = parse_calendar_callback(data)
        if action == "ignore":
            return
        if action == "nav":
            y, m = map(int, payload.split(":"))
            new_kb = create_calendar(y, m, prefix="outcal_home_out", min_date=datetime.now().date())
            await query.edit_message_reply_markup(reply_markup=new_kb)
            return
        if action == "sel":
            flow["out_date"] = payload  # YYYY-MM-DD
            await query.edit_message_text(
                f"🏡 <b>Home Outing — Out Date & Time</b>\n\n"
                f"📅 Date: <b>{_friendly_date(payload)}</b>\n\n"
                f"⏰ <b>Select Departure Hour:</b>\n"
                f"Tap your departure hour below (0:00 — 23:00):",
                parse_mode="HTML",
                reply_markup=create_hour_picker("home_out", payload),
            )
            return

    if data.startswith("outcal_home_in:"):
        action, payload = parse_calendar_callback(data)
        if action == "ignore":
            return
        if action == "nav":
            y, m = map(int, payload.split(":"))
            out_d = datetime.strptime(flow.get("out_date", str(date.today())), "%Y-%m-%d").date()
            new_kb = create_calendar(y, m, prefix="outcal_home_in", min_date=out_d)
            await query.edit_message_reply_markup(reply_markup=new_kb)
            return
        if action == "sel":
            flow["in_date"] = payload  # YYYY-MM-DD
            await query.edit_message_text(
                f"🏡 <b>Home Outing — In Date & Time</b>\n\n"
                f"📅 Return Date: <b>{_friendly_date(payload)}</b>\n\n"
                f"⏰ <b>Select Return Hour:</b>\n"
                f"Tap your expected return hour below (0:00 — 23:00):",
                parse_mode="HTML",
                reply_markup=create_hour_picker("home_in", payload),
            )
            return

    # ==========================================
    # 4. HOUR & MINUTE PICKERS (ERP STYLE)
    # ==========================================
    if data.startswith("outpass:hr:"):
        parts = data.split(":")
        step = parts[2]
        hour = int(parts[3])
        flow[f"{step}_hour"] = hour
        date_str = flow.get("out_date" if "out" in step else "in_date") or flow.get("date", str(date.today()))
        title = "Out Date & Time" if "out" in step else "In Date & Time"
        await query.edit_message_text(
            f"⏰ <b>{title}</b>\n\n"
            f"📅 Date: <b>{_friendly_date(date_str)}</b>\n"
            f"🕐 Selected Hour: <b>{hour}:00</b>\n\n"
            f"⏱️ <b>Select Exact Minute:</b>",
            parse_mode="HTML",
            reply_markup=create_minute_picker(step, hour, date_str),
        )
        return

    if data.startswith("outpass:back_hr:"):
        step = data.split(":")[2]
        date_str = flow.get("out_date" if "out" in step else "in_date") or flow.get("date", str(date.today()))
        title = "Out Date & Time" if "out" in step else "In Date & Time"
        await query.edit_message_text(
            f"⏰ <b>{title}</b>\n\n"
            f"📅 Date: <b>{_friendly_date(date_str)}</b>\n\n"
            f"🕐 <b>Select Hour:</b>\n"
            f"Tap your hour below (0:00 — 23:00):",
            parse_mode="HTML",
            reply_markup=create_hour_picker(step, date_str),
        )
        return

    if data.startswith("outpass:min:"):
        parts = data.split(":")
        step = parts[2]
        time_str = f"{parts[3]}:{parts[4]}:{parts[5]}"  # HH:MM:SS

        if step == "home_out":
            flow["out_time"] = time_str
            out_d = datetime.strptime(flow["out_date"], "%Y-%m-%d").date()
            in_cal_kb = create_calendar(out_d.year, out_d.month, prefix="outcal_home_in", min_date=out_d)
            await query.edit_message_text(
                f"🏡 <b>Home Outing — Step 3 of 5</b>\n\n"
                f"🛫 <b>Out Date & Time:</b> <code>{_friendly_date(flow['out_date'])} {_format_time_friendly(time_str)}</code>\n\n"
                f"📅 <b>Select Return Date (In-Date):</b>\n"
                f"Tap your expected return date on the calendar below:",
                parse_mode="HTML",
                reply_markup=in_cal_kb,
            )
            return

        if step == "home_in":
            flow["in_time"] = time_str
            context.user_data["login_state"] = STATE_WAITING_OUTPASS_REASON
            buttons = [[InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel")]]
            await query.edit_message_text(
                f"🏠 <b>Home Outing — Step 5 of 5</b>\n\n"
                f"🛫 <b>Out Date & Time:</b> <code>{_friendly_date(flow['out_date'])} {_format_time_friendly(flow['out_time'])}</code>\n"
                f"🛬 <b>In Date & Time:</b> <code>{_friendly_date(flow['in_date'])} {_format_time_friendly(time_str)}</code>\n\n"
                f"✍️ <b>Reason for Outing:</b>\n"
                f"Please <b>type your reason</b> directly in this chat and send it:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if step == "loc_out":
            flow["out_time"] = time_str
            await query.edit_message_text(
                f"⏱️ <b>Local Outing — Step 3 of 5</b>\n\n"
                f"📅 Date: <b>{_friendly_date(flow.get('date', ''))}</b>\n"
                f"⏰ Out Time: <b>{_format_time_friendly(time_str)}</b>\n\n"
                f"🕐 <b>Select Return Hour (In-Time):</b>\n"
                f"Tap your return hour below (0:00 — 23:00):",
                parse_mode="HTML",
                reply_markup=create_hour_picker("loc_in", flow.get("date", "")),
            )
            return

        if step == "loc_in":
            flow["in_time"] = time_str
            await _show_meals_keyboard(query, flow)
            return

    if data.startswith("outpass:toggle_meal:"):
        meal_name = data.split(":", 2)[2]
        meals = flow.setdefault("meals", {"breakfast": "NO", "lunch": "NO", "snack": "NO", "dinner": "NO"})
        meals[meal_name] = "YES" if meals.get(meal_name, "NO") == "NO" else "NO"
        await _show_meals_keyboard(query, flow, is_edit_markup_only=True)
        return

    if data == "outpass:meals_done":
        context.user_data["login_state"] = STATE_WAITING_OUTPASS_REASON
        buttons = [[InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel")]]
        await query.edit_message_text(
            f"⏱️ <b>Local Outing — Step 5 of 5</b>\n\n"
            f"📅 <b>Date:</b> <code>{_friendly_date(flow.get('date', ''))}</code>\n"
            f"⏰ <b>Timing:</b> <code>{_format_time_friendly(flow.get('out_time'))} ➔ {_format_time_friendly(flow.get('in_time'))}</code>\n\n"
            f"✍️ <b>Reason for Outing:</b>\n"
            f"Please <b>type your reason</b> directly in this chat and send it:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ==========================================
    # 5. REASON & CONFIRMATION REVIEW
    # ==========================================
    if data.startswith("outpass:reason:"):
        reason_text = data.split(":", 2)[2]
        flow["reason"] = reason_text
        context.user_data.pop("login_state", None)
        await _show_confirmation_review(query.message, flow)
        return

    # ==========================================
    # 6. SUBMIT TO ERP / SAVE
    # ==========================================
    if data == "outpass:confirm_local":
        if not flow:
            await query.edit_message_text("❌ <b>Session expired.</b> Please apply again via /outpass.", parse_mode="HTML")
            return

        async with user_concurrency.task_guard(uid) as acquired:
            if not acquired:
                await query.answer("⏳ Outpass submission in progress. Please wait...", show_alert=True)
                return

            # Pop flow immediately to prevent any race conditions or double submissions
            context.user_data.pop("outpass_flow", None)
            await query.edit_message_text("⏳ <b>Submitting Local Outpass to Warden...</b>", parse_mode="HTML")

            date_str = flow.get("date")
            out_time = flow.get("out_time")
            in_time = flow.get("in_time")
            reason = flow.get("reason", "Local Outing")
            meals = flow.get("meals", {})

            saved = credentials.load_user_credentials(uid)
            sess = load_session(user_id=uid)
            if saved and sess and sess.get("cookies"):
                success, msg = await outpass_api.submit_local_outpass(
                    date=date_str,
                    out_time=out_time,
                    in_time=in_time,
                    reason=reason,
                    meals=meals,
                    user_id=uid,
                )
            else:
                import random
                test_id = str(random.randint(40000, 49999))
                _test_outpass_store[uid] = [{
                    "id": test_id,
                    "student_id": str(uid),
                    "outing_date": date_str,
                    "out_time": f"{date_str} {out_time}",
                    "in_time": f"{date_str} {in_time}",
                    "reason": reason,
                    "warden_name": "Dr. K. Srinivas (Chief Warden)",
                    "status": "Approved",
                    "approved_by": "Dr. K. Srinivas (Chief Warden)",
                }]
                success, msg = True, "Simulated in Test Mode"

            if success:
                outpasses = []
                if saved and sess and sess.get("cookies"):
                    try:
                        outpasses = await outpass_api.fetch_outpass_history(user_id=uid)
                    except Exception:
                        outpasses = []
                if not outpasses:
                    outpasses = _test_outpass_store.get(uid, [])

                status_card = format_outpass_history_message(outpasses)
                buttons = [
                    [
                        InlineKeyboardButton("🔄 Refresh Status", callback_data="outpass:refresh"),
                        InlineKeyboardButton("➕ Apply New Outpass", callback_data="outpass:start"),
                    ]
                ]
                if uid in _test_outpass_store:
                    buttons.append([
                        InlineKeyboardButton("🧪 Toggle Status (Approved ⇄ Pending)", callback_data="outpass:toggle_test_status")
                    ])
                    buttons.append([
                        InlineKeyboardButton("🗑️ Clear / Reset Test Pass", callback_data="outpass:clear_test_status")
                    ])
                await query.edit_message_text(
                    f"✅ <b>Outpass Submitted & Applied Successfully!</b>\n\n{status_card}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            else:
                await query.edit_message_text(f"❌ <b>Outpass Failed:</b>\n{msg}", parse_mode="HTML")
            return

    if data == "outpass:confirm_home":
        if not flow:
            await query.edit_message_text("❌ <b>Session expired.</b> Please apply again via /outpass.", parse_mode="HTML")
            return

        async with user_concurrency.task_guard(uid) as acquired:
            if not acquired:
                await query.answer("⏳ Outpass submission in progress. Please wait...", show_alert=True)
                return

            # Pop flow immediately to prevent any race conditions or double submissions
            context.user_data.pop("outpass_flow", None)
            await query.edit_message_text("⏳ <b>Submitting Home Outpass to Warden...</b>", parse_mode="HTML")

            out_dt = f"{flow.get('out_date')} {flow.get('out_time')}"
            in_dt = f"{flow.get('in_date')} {flow.get('in_time')}"
            reason = flow.get("reason", "Home Outing")

            saved = credentials.load_user_credentials(uid)
            sess = load_session(user_id=uid)
            if saved and sess and sess.get("cookies"):
                success, msg = await outpass_api.submit_home_outpass(
                    out_datetime=out_dt,
                    in_datetime=in_dt,
                    reason=reason,
                    user_id=uid,
                )
            else:
                import random
                test_id = str(random.randint(50000, 59999))
                _test_outpass_store[uid] = [{
                    "id": test_id,
                    "student_id": str(uid),
                    "outing_date": flow.get('out_date'),
                    "out_time": out_dt,
                    "in_time": in_dt,
                    "reason": reason,
                    "warden_name": "Dr. K. Srinivas (Chief Warden)",
                    "status": "Approved",
                    "approved_by": "Dr. K. Srinivas (Chief Warden)",
                }]
                success, msg = True, "Simulated in Test Mode"

            if success:
                outpasses = []
                if saved and sess and sess.get("cookies"):
                    try:
                        outpasses = await outpass_api.fetch_outpass_history(user_id=uid)
                    except Exception:
                        outpasses = []
                if not outpasses:
                    outpasses = _test_outpass_store.get(uid, [])

                status_card = format_outpass_history_message(outpasses)
                buttons = [
                    [
                        InlineKeyboardButton("🔄 Refresh Status", callback_data="outpass:refresh"),
                        InlineKeyboardButton("➕ Apply New Outpass", callback_data="outpass:start"),
                    ]
                ]
                if uid in _test_outpass_store:
                    buttons.append([
                        InlineKeyboardButton("🧪 Toggle Status (Approved ⇄ Pending)", callback_data="outpass:toggle_test_status")
                    ])
                    buttons.append([
                        InlineKeyboardButton("🗑️ Clear / Reset Test Pass", callback_data="outpass:clear_test_status")
                    ])
                await query.edit_message_text(
                    f"✅ <b>Outpass Submitted & Applied Successfully!</b>\n\n{status_card}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            else:
                await query.edit_message_text(f"❌ <b>Outpass Failed:</b>\n{msg}", parse_mode="HTML")
            return


async def _show_meals_keyboard(query, flow: dict, is_edit_markup_only: bool = False) -> None:
    meals = flow.setdefault("meals", {"breakfast": "NO", "lunch": "NO", "snack": "NO", "dinner": "NO"})

    def _mark(val):
        return "✅ YES" if val == "YES" else "❌ NO"

    buttons = [
        [
            InlineKeyboardButton(f"🍞 Breakfast: {_mark(meals['breakfast'])}", callback_data="outpass:toggle_meal:breakfast"),
            InlineKeyboardButton(f"🍛 Lunch: {_mark(meals['lunch'])}", callback_data="outpass:toggle_meal:lunch"),
        ],
        [
            InlineKeyboardButton(f"☕ Snack: {_mark(meals['snack'])}", callback_data="outpass:toggle_meal:snack"),
            InlineKeyboardButton(f"🍽️ Dinner: {_mark(meals['dinner'])}", callback_data="outpass:toggle_meal:dinner"),
        ],
        [
            InlineKeyboardButton("➡️ Next (Reason) ➔", callback_data="outpass:meals_done")
        ],
        [
            InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel")
        ]
    ]

    kb = InlineKeyboardMarkup(buttons)
    if is_edit_markup_only:
        await query.edit_message_reply_markup(reply_markup=kb)
    else:
        text = (
            f"⏱️ <b>Local Outing — Step 4 of 5</b>\n\n"
            f"📅 Date: <b>{_friendly_date(flow.get('date', ''))}</b>\n"
            f"⏰ Timing: <b>{_format_time_friendly(flow.get('out_time'))} ➔ {_format_time_friendly(flow.get('in_time'))}</b>\n\n"
            f"🍽️ <b>Mess Meals Preference:</b>\n"
            f"Tap to toggle which meals you will eat in the hostel:"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def _show_confirmation_review(message, flow: dict) -> None:
    op_type = flow.get("type", "days")
    if op_type == "hours":
        meals = flow.get("meals", {})
        meals_str = f"Breakfast: {meals.get('breakfast')}, Lunch: {meals.get('lunch')}, Dinner: {meals.get('dinner')}"
        text = (
            "🏠 <b>Review Outpass Application</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Category:</b> ⏱️ Local Outing (Hours)\n"
            f"• <b>Date:</b> <code>{_friendly_date(flow.get('date', ''))}</code>\n"
            f"• <b>Out Date & Time:</b> <code>{_friendly_date(flow.get('date', ''))} {_format_time_friendly(flow.get('out_time'))}</code>\n"
            f"• <b>In Date & Time:</b> <code>{_friendly_date(flow.get('date', ''))} {_format_time_friendly(flow.get('in_time'))}</code>\n"
            f"• <b>Reason:</b> <i>{flow.get('reason')}</i>\n"
            f"• <b>Mess:</b> {meals_str}\n\n"
            "⚠️ <b>Note:</b> Your outpass is <b>NOT</b> applied yet.\n"
            "👉 Press <b>Submit Outpass</b> below to officially apply:"
        )
        buttons = [
            [InlineKeyboardButton("✅ Submit Outpass", callback_data="outpass:confirm_local")],
            [InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel")],
        ]
    else:
        text = (
            "🏠 <b>Review Outpass Application</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "• <b>Category:</b> 🏡 Home Outing (Days)\n"
            f"• <b>Out Date & Time:</b> <code>{_friendly_date(flow.get('out_date', ''))} {_format_time_friendly(flow.get('out_time'))}</code>\n"
            f"• <b>In Date & Time:</b> <code>{_friendly_date(flow.get('in_date', ''))} {_format_time_friendly(flow.get('in_time'))}</code>\n"
            f"• <b>Reason:</b> <i>{flow.get('reason')}</i>\n\n"
            "⚠️ <b>Note:</b> Your outpass is <b>NOT</b> applied yet.\n"
            "👉 Press <b>Submit Outpass</b> below to officially apply:"
        )
        buttons = [
            [InlineKeyboardButton("✅ Submit Outpass", callback_data="outpass:confirm_home")],
            [InlineKeyboardButton("❌ Cancel Outpass", callback_data="outpass:cancel")],
        ]

    await message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def handle_outpass_text_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If user is typing a custom outpass reason, process it and return True."""
    if context.user_data.get("login_state") != STATE_WAITING_OUTPASS_REASON:
        return False

    text = update.message.text.strip()
    if len(text) > 150:
        text = text[:150]
    flow = context.user_data.setdefault("outpass_flow", {})
    flow["reason"] = text
    context.user_data.pop("login_state", None)

    msg = await update.message.reply_text("⏳ <i>Preparing review...</i>", parse_mode="HTML")
    await _show_confirmation_review(msg, flow)
    return True
