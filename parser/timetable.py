import re
from typing import List, Optional

from bs4 import BeautifulSoup, Tag


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

    # grid will be built row by row; cells from rowspan above are inserted.
    grid: List[List[str]] = []
    # pending[rowspan_origin_row][col] = (remaining_rows, text)
    pending: dict = {}

    for r_idx, tr in enumerate(raw_rows):
        cells = tr.find_all(["td", "th"])
        row: List[str] = []
        c_idx = 0

        for cell in cells:
            # Skip columns already occupied by rowspans from previous rows.
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

        # Fill any remaining columns carried by rowspan.
        while c_idx in pending and pending[c_idx][0] > 0:
            row.append(pending[c_idx][1])
            pending[c_idx] = (pending[c_idx][0] - 1, pending[c_idx][1])
            c_idx += 1

        if row:
            grid.append(row)

    return grid


def _is_day_name(text: str) -> bool:
    return text.lower() in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


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
        # If this row's first cell is a day name, rows before it are headers.
        if _is_day_name(row[0]):
            data_start = i
            header_idx = max(0, i - 1)
            break

    return header_idx, list(range(data_start, len(grid)))


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

    # Always keep the first column (day names) even if it looks empty somehow.
    if keep and keep[0] != 0:
        keep.insert(0, 0)

    return [[row[c] if c < len(row) else "" for c in keep] for row in grid]


def _format_day(day: str, periods: List[str], header: List[str]) -> str:
    """Format one day's classes as a compact mobile-friendly list."""
    lines = [f"*{day}*"]
    has_any = False

    i = 0
    while i < len(periods):
        cell = periods[i].strip()
        period_label = header[i] if i < len(header) else str(i + 1)

        if not cell or cell == "-":
            i += 1
            continue

        # Group consecutive identical classes.
        j = i + 1
        while j < len(periods) and periods[j].strip() == cell:
            j += 1

        end_label = header[j - 1] if j - 1 < len(header) else str(j)
        if j - i > 1 and period_label != end_label:
            lines.append(f"  Periods {period_label}-{end_label}: {cell}")
        else:
            lines.append(f"  Period {period_label}: {cell}")
        has_any = True
        i = j

    if not has_any:
        lines.append("  No classes")

    return "\n".join(lines)


def parse_timetable_html(html: str) -> str:
    """
    Parse the ERP timetable HTML and return a Telegram-friendly formatted string.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove script/style/nav elements that might pollute text extraction.
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    tables = soup.find_all("table")
    if not tables:
        return "No timetable table found in the ERP response."

    # Heuristic: pick the largest table (most rows/cells) as the timetable.
    best_table = max(tables, key=lambda t: len(t.find_all(["td", "th"])))

    grid = _parse_table_to_grid(best_table)
    if not grid:
        return "Timetable table is empty."

    header_idx, data_indices = _find_header_and_data_rows(grid)
    header = grid[header_idx] if header_idx < len(grid) else []
    grid = _drop_empty_columns(grid, data_indices)

    # Recompute header/data after dropping columns.
    header_idx, data_indices = _find_header_and_data_rows(grid)
    header = grid[header_idx] if header_idx < len(grid) else []

    sections = []
    for r in data_indices:
        row = grid[r]
        if not row:
            continue
        day = row[0] if row[0] else f"Row {r}"
        periods = row[1:]
        sections.append(_format_day(day, periods, header[1:] if len(header) > 1 else []))

    if not sections:
        return "Could not extract timetable rows."

    return "\n\n".join(sections)


def format_timetable_for_telegram(html: str) -> str:
    text = parse_timetable_html(html)
    # Telegram messages have a limit of 4096 characters.
    if len(text) > 3900:
        text = text[:3897] + "..."
    return f"📅 *Your Timetable*\n\n{text}"
