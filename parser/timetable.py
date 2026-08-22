import html as html_lib
import re
from datetime import datetime
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

import config


# Course Code to Full Course Name Catalog.
COURSE_CATALOG = {
    "24CC3014": "AWS Certified Solutions Architect - Associate (SAA-C03)",
    "24CND3101": "Cloud-Native App Dev & Container Orchestration (CNAD)",
    "24CI3201": "Adaptive Software Engineering",
    "24ACI3507F": "Reinforcement Learning",
    "24AD3107": "Data Science",
    "24CS3105": "Information Assurance and Security",
    "24GMI3202HF": "Engineering Large Language Model Systems",
    "24SDCS03A": "SOA Programming and Microservices",
}


def _get_course_title(raw_code: str) -> Optional[str]:
    """Resolve a raw course code (e.g. '24GMI3202HF-P' or '24SDCS03A-L') to its human-readable title."""
    c = raw_code.strip()
    # Strip suffixes like -P, -L, -S, -T, -PR, -PROJ, -I, -V
    cleaned = re.sub(r"[-_\s]+(?:L|P|S|T|PR|PROJ|I|V|Lecture|Lab|Practical|Skill|Tutorial)$", "", c, flags=re.IGNORECASE).strip()

    for code, title in COURSE_CATALOG.items():
        if code.upper() == cleaned.upper() or code.upper() == c.upper():
            return title
    return None


def _get_type_info(subject: str) -> Tuple[str, str]:
    """
    Return appropriate (emoji, label) for a given class type:
    - Lecture: Teacher emoji (👨‍🏫)
    - Practical / Lab / Skill Lab: PC / Laptop emoji (💻)
    - Tutorial: Notes emoji (📝)
    - Project: Tools emoji (🛠️)
    - Internship: Office emoji (🏢)
    """
    upper = subject.upper()
    if upper.endswith("-S") or "SKILL" in upper:
        return "💻", "Skill Lab"
    elif upper.endswith("-P") or "LAB" in upper or "PRACTICAL" in upper:
        return "💻", "Practical Lab"
    elif upper.endswith("-L") or "LECTURE" in upper:
        return "👨‍🏫", "Lecture"
    elif upper.endswith("-T") or "TUTORIAL" in upper:
        return "📝", "Tutorial"
    elif upper.endswith("-PR") or upper.endswith("-PROJ") or "PROJECT" in upper:
        return "🛠️", "Project"
    elif upper.endswith("-I") or "INTERN" in upper:
        return "🏢", "Internship"
    return "📚", "Class"


# KL University class slot timings.
SLOT_TIMINGS = {
    "1": ("07:10", "08:00"),
    "2": ("08:00", "08:50"),
    "3": ("09:20", "10:10"),
    "4": ("10:10", "11:00"),
    "5": ("11:10", "12:00"),
    "6": ("12:00", "12:50"),
    "7": ("13:00", "13:50"),
    "8": ("13:50", "14:40"),
    "9": ("14:50", "15:40"),
    "10": ("15:50", "16:40"),
    "11": ("16:40", "17:30"),
}


def _to_12h(time_str: str) -> str:
    """Convert 24h time like '07:10' or '13:50' to 12h format like '07:10 AM' or '01:50 PM'."""
    t = time_str.strip()
    if "AM" in t or "PM" in t:
        return t
    parts = t.split(":")
    if len(parts) == 2:
        try:
            h, m = int(parts[0]), int(parts[1])
            suffix = "AM" if h < 12 else "PM"
            h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
            return f"{h12:02d}:{m:02d} {suffix}"
        except ValueError:
            return time_str
    return time_str


def _slot_time_range(slot_label: str) -> str:
    """Convert a slot number label like '3' into a time range like '09:20 AM-10:10 AM'."""
    slot = str(slot_label).strip()
    if slot in SLOT_TIMINGS:
        start, end = SLOT_TIMINGS[slot]
        return f"{_to_12h(start)}-{_to_12h(end)}"
    return f"Slot {slot}"


_DAY_MAP = {
    "mon": "Mon",
    "monday": "Mon",
    "tue": "Tue",
    "tuesday": "Tue",
    "wed": "Wed",
    "wednesday": "Wed",
    "thu": "Thu",
    "thursday": "Thu",
    "fri": "Fri",
    "friday": "Fri",
    "sat": "Sat",
    "saturday": "Sat",
    "sun": "Sun",
    "sunday": "Sun",
}

_DAY_FULL_NAMES = {
    "Mon": "Monday",
    "Tue": "Tuesday",
    "Wed": "Wednesday",
    "Thu": "Thursday",
    "Fri": "Friday",
    "Sat": "Saturday",
    "Sun": "Sunday",
}


def _normalize_day_name(day: str) -> str:
    """Normalize a day name to a short title-case form, e.g. 'monday' -> 'Mon'."""
    if not day:
        return ""
    d = day.strip().lower()
    if d in _DAY_MAP:
        return _DAY_MAP[d]
    m = re.search(r"\b(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", d, re.IGNORECASE)
    if m:
        return _DAY_MAP.get(m.group(1).lower(), day.strip())
    return day.strip()


def _ist_now() -> datetime:
    """Return the current datetime in IST timezone."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Kolkata")
    except Exception:
        tz = None
    return datetime.now(tz)


def _today_day_name() -> str:
    """Return today's day name in short form (e.g. 'Fri') using IST timezone."""
    return _ist_now().strftime("%a")


def _offset_day_name(days: int) -> str:
    """Return the short day name for today + ``days`` offset."""
    from datetime import timedelta
    return (_ist_now() + timedelta(days=days)).strftime("%a")


def resolve_day_name(arg: Optional[str]) -> Optional[str]:
    """
    Resolve a user-provided day argument to a short day name.

    Supports: today, yesterday, tomorrow, mon/monday, tue/tuesday, etc.
    Returns None for an unrecognized argument.
    """
    if not arg:
        return _today_day_name()

    arg = arg.strip().lower()
    if arg in {"today", "now"}:
        return _today_day_name()
    if arg == "yesterday":
        return _offset_day_name(-1)
    if arg == "tomorrow":
        return _offset_day_name(1)

    normalized = _normalize_day_name(arg)
    if normalized.lower() in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
        return normalized

    return None


def _clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_table_to_grid(table: Tag) -> List[List[str]]:
    """
    Parse an HTML table into a 2D grid, accounting for rowspan and colspan.
    """
    raw_rows = table.find_all("tr")
    if not raw_rows:
        return []

    grid: List[List[str]] = []
    pending: dict = {}

    for r_idx, tr in enumerate(raw_rows):
        cells = tr.find_all(["td", "th"])
        row: List[str] = []
        c_idx = 0

        for cell in cells:
            while c_idx in pending and pending[c_idx][0] > 0:
                row.append(pending[c_idx][1])
                pending[c_idx] = (pending[c_idx][0] - 1, pending[c_idx][1])
                c_idx += 1

            text = _clean_text(cell.get_text(separator=" ", strip=True))
            colspan = max(1, int(cell.get("colspan", 1) or 1))
            rowspan = max(1, int(cell.get("rowspan", 1) or 1))

            for offset in range(colspan):
                row.append(text)
                if rowspan > 1:
                    pending[c_idx + offset] = (rowspan - 1, text)

            c_idx += colspan

        while c_idx in pending and pending[c_idx][0] > 0:
            row.append(pending[c_idx][1])
            pending[c_idx] = (pending[c_idx][0] - 1, pending[c_idx][1])
            c_idx += 1

        if row:
            grid.append(row)

    return grid


def _is_day_name(text: str) -> bool:
    if not text:
        return False
    return _normalize_day_name(text).lower() in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _find_header_and_data_rows(grid: List[List[str]]) -> tuple:
    """
    Heuristic: find the last row before data rows that contain day names.
    Returns (header_row_index, data_row_indices).
    """
    header_idx = 0
    data_start = 1

    for i, row in enumerate(grid):
        if not row:
            continue
        if _is_day_name(row[0]):
            data_start = i
            header_idx = max(0, i - 1)
            break

    data_indices = [
        i for i in range(data_start, len(grid))
        if grid[i] and _is_day_name(grid[i][0])
    ]
    if not data_indices:
        data_indices = list(range(data_start, len(grid)))

    return header_idx, data_indices


def _drop_empty_columns(grid: List[List[str]], data_indices: List[int]) -> List[List[str]]:
    """Remove columns that are empty in every data row."""
    if not grid or not data_indices:
        return grid

    max_cols = max(len(row) for row in grid)
    keep = []
    for c in range(max_cols):
        has_content = False
        for r in data_indices:
            if c < len(grid[r]) and grid[r][c].strip() and grid[r][c].strip() != "-":
                has_content = True
                break
        if has_content:
            keep.append(c)

    if keep and keep[0] != 0:
        keep.insert(0, 0)

    return [[row[c] if c < len(row) else "" for c in keep] for row in grid]


def _parse_period_cell(cell: str) -> dict:
    """
    Parse a raw timetable cell (e.g. '24CC3014-S - S-51 -RoomNo-C318A') into
    subject code, section, room, and class type.
    """
    cell = cell.strip()
    if not cell or cell == "-":
        return {"subject": "", "section": "", "room": ""}

    room = ""
    # Extract RoomNo if present (e.g. -RoomNo-C618, - RoomNo - C618, RoomNo: C618)
    room_match = re.search(r"[-–—\s]*Room\s*No[-:\s]*(?P<room>[A-Za-z0-9_\-]+)", cell, re.IGNORECASE)
    if room_match:
        room = room_match.group("room").strip().rstrip("- ")
        cell_without_room = cell[:room_match.start()] + cell[room_match.end():]
    else:
        cell_without_room = cell

    # Split remaining segments by spaced hyphens or slashes (preserving internal hyphens like 24AD3107-P, S-52)
    parts = [p.strip() for p in re.split(r"\s+[-–—/]\s+|\s*[/]\s*", cell_without_room) if p.strip()]

    subject = ""
    section = ""

    if len(parts) >= 2:
        subject = parts[0]
        section = parts[1]
    elif len(parts) == 1:
        subject = parts[0]
    else:
        subject = cell

    return {
        "subject": subject.strip(),
        "section": section.strip(),
        "room": room.strip(),
    }


def _format_day(day: str, periods: List[str], header: List[str]) -> str:
    """Format one day's classes as an easy-to-read, beautifully badged Telegram card block."""
    blocks: List[str] = []

    i = 0
    while i < len(periods):
        cell = periods[i].strip()
        period_label = header[i] if i < len(header) else str(i + 1)

        if not cell or cell == "-":
            i += 1
            continue

        # Group consecutive identical classes (e.g. 2 or 3 slot practicals/skilling).
        j = i + 1
        while j < len(periods) and periods[j].strip() == cell:
            j += 1

        end_label = header[j - 1] if j - 1 < len(header) else str(j)
        start_time = _slot_time_range(period_label).split("-")[0]
        end_time = _slot_time_range(end_label).split("-")[-1]

        parsed = _parse_period_cell(cell)
        raw_subj = parsed["subject"]
        course_title = _get_course_title(raw_subj)
        type_emoji, type_label = _get_type_info(raw_subj)

        room = html_lib.escape(parsed["room"]) if parsed["room"] else "TBA"
        section = html_lib.escape(parsed["section"]) if parsed["section"] else "—"

        slot_span = j - i
        span_badge = f" <i>({slot_span} slots)</i>" if slot_span > 1 else ""

        # If we have a course title, display title and type with emoji
        if course_title:
            title_escaped = html_lib.escape(course_title)
            code_escaped = html_lib.escape(raw_subj)
            content_line = f"<b>{title_escaped}</b>\n🏷️ <code>{code_escaped}</code> • <i>{type_label}</i> {type_emoji}"
        else:
            code_escaped = html_lib.escape(raw_subj)
            content_line = f"<b>{code_escaped}</b>\n🏷️ <i>{type_label}</i> {type_emoji}"

        card = (
            f"<blockquote>"
            f"🕐 <b>{start_time} – {end_time}</b>{span_badge}\n"
            f"{content_line}\n"
            f"📍 <b>Room:</b> <code>{room}</code>  •  👥 <b>Sec:</b> <code>{section}</code>"
            f"</blockquote>"
        )
        blocks.append(card)
        i = j

    norm_day = _normalize_day_name(day)
    full_day_name = _DAY_FULL_NAMES.get(norm_day, day).upper()

    if not blocks:
        return (
            f"📅 <b>{full_day_name} SCHEDULE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏖️ <i>No classes scheduled! Free day.</i>"
        )

    count_str = f"{len(blocks)} {'Class' if len(blocks) == 1 else 'Classes'}"
    body = "\n\n".join(blocks)
    return (
        f"📅 <b>{full_day_name} SCHEDULE</b>  •  <code>{count_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body}"
    )


def parse_timetable_sections(html: str, day_filter: Optional[str] = None) -> List[Tuple[str, str]]:
    """
    Parse the ERP timetable HTML and return a list of (day_name, formatted_day_schedule) tuples.
    If ``day_filter`` is provided, only the matching day is returned.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove script/style/nav elements.
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    tables = soup.find_all("table")
    if not tables:
        return []

    best_table = max(tables, key=lambda t: len(t.find_all(["td", "th"])))
    grid = _parse_table_to_grid(best_table)
    if not grid:
        return []

    header_idx, data_indices = _find_header_and_data_rows(grid)
    header = grid[header_idx] if header_idx < len(grid) else []
    grid = _drop_empty_columns(grid, data_indices)

    header_idx, data_indices = _find_header_and_data_rows(grid)
    header = grid[header_idx] if header_idx < len(grid) else []

    normalized_filter = _normalize_day_name(day_filter) if day_filter else None

    sections: List[Tuple[str, str]] = []
    for r in data_indices:
        row = grid[r]
        if not row:
            continue
        first_cell = row[0] if len(row) > 0 else ""
        if not _is_day_name(first_cell):
            continue
        day = _normalize_day_name(first_cell)
        if normalized_filter and day != normalized_filter:
            continue
        periods = row[1:]
        sections.append((day, _format_day(day, periods, header[1:] if len(header) > 1 else [])))

    return sections


def parse_timetable_html(html: str, day_filter: Optional[str] = None) -> str:
    """
    Parse the ERP timetable HTML and return an aesthetically formatted string.

    If ``day_filter`` is provided, only the matching day is returned.
    """
    sections = parse_timetable_sections(html, day_filter=day_filter)

    if not sections:
        if day_filter:
            normalized_filter = _normalize_day_name(day_filter)
            full_name = _DAY_FULL_NAMES.get(normalized_filter, day_filter).upper()
            return (
                f"📅 <b>{full_name} SCHEDULE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏖️ <i>No classes scheduled! Free day.</i>"
            )
        return "⚠️ <i>Could not extract timetable rows from ERP response.</i>"

    # Separate days with a clean divider in week view
    return "\n\n━━━━━━━━━━━━━━━━━━━━━━\n\n".join(text for _, text in sections)


def get_timetable_messages(html: str, day_filter: Optional[str] = None, max_length: int = 3800) -> List[str]:
    """
    Parse timetable and partition formatted days into chunks <= max_length characters
    to prevent exceeding Telegram message limits (4096) or breaking HTML formatting.
    """
    sections = parse_timetable_sections(html, day_filter=day_filter)

    if not sections:
        if day_filter:
            normalized_filter = _normalize_day_name(day_filter)
            full_name = _DAY_FULL_NAMES.get(normalized_filter, day_filter).upper()
            return [(
                f"📅 <b>{full_name} SCHEDULE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏖️ <i>No classes scheduled! Free day.</i>"
            )]
        return ["⚠️ <i>Could not extract timetable schedule from ERP response.</i>"]

    messages: List[str] = []
    current_chunk: List[str] = []
    current_len = 0
    divider = "\n\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    divider_len = len(divider)

    for _, day_text in sections:
        day_len = len(day_text)
        if not current_chunk:
            current_chunk.append(day_text)
            current_len = day_len
        elif current_len + divider_len + day_len <= max_length:
            current_chunk.append(day_text)
            current_len += divider_len + day_len
        else:
            messages.append(divider.join(current_chunk))
            current_chunk = [day_text]
            current_len = day_len

    if current_chunk:
        messages.append(divider.join(current_chunk))

    return messages


def format_timetable_for_telegram(html: str, day: Optional[str] = None) -> str:
    """Format the timetable HTML for Telegram display without breaking HTML tags."""
    messages = get_timetable_messages(html, day_filter=day)
    return messages[0] if messages else "⚠️ <i>Timetable is empty.</i>"
