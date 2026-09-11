import calendar
from datetime import date, datetime
from typing import Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]


def create_calendar(
    year: Optional[int] = None,
    month: Optional[int] = None,
    prefix: str = "outcal",
    min_date: Optional[date] = None,
    max_days_ahead: int = 60,
) -> InlineKeyboardMarkup:
    """
    Generate an aesthetic Telegram inline keyboard calendar for the given year and month.
    """
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    if min_date is None:
        min_date = now.date()

    keyboard = []

    # Row 1: Navigation header [ < ] [ Month Year ] [ > ]
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1

    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    header_buttons = [
        InlineKeyboardButton("◀️", callback_data=f"{prefix}:nav:{prev_year}:{prev_month}"),
        InlineKeyboardButton(f"📅 {MONTH_NAMES[month]} {year}", callback_data=f"{prefix}:ignore"),
        InlineKeyboardButton("▶️", callback_data=f"{prefix}:nav:{next_year}:{next_month}"),
    ]
    keyboard.append(header_buttons)

    # Row 2: Days of week header
    week_days = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    keyboard.append([InlineKeyboardButton(day, callback_data=f"{prefix}:ignore") for day in week_days])

    # Month calendar grid
    cal = calendar.monthcalendar(year, month)
    today = now.date()

    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data=f"{prefix}:ignore"))
            else:
                cur_date = date(year, month, day)
                # Check if date is in the past
                if min_date and cur_date < min_date:
                    row.append(InlineKeyboardButton("·", callback_data=f"{prefix}:ignore"))
                else:
                    label = f"[{day}]" if cur_date == today else str(day)
                    date_str = f"{year:04d}-{month:02d}-{day:02d}"
                    row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:sel:{date_str}"))
        keyboard.append(row)

    # Close / Cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel Outpass", callback_data=f"{prefix}:cancel")])

    return InlineKeyboardMarkup(keyboard)


def parse_calendar_callback(callback_data: str) -> Tuple[str, Optional[str]]:
    """
    Parse a calendar callback data string.
    Returns (action, payload) where:
      - action in ('sel', 'nav', 'cancel', 'ignore')
      - payload is date string (for 'sel'), or 'YYYY:MM' (for 'nav'), or None
    """
    parts = callback_data.split(":")
    if len(parts) < 2:
        return "ignore", None

    action = parts[1]
    if action == "sel" and len(parts) >= 3:
        return "sel", parts[2]
    elif action == "nav" and len(parts) >= 4:
        return "nav", f"{parts[2]}:{parts[3]}"
    elif action == "cancel":
        return "cancel", None

    return "ignore", None
