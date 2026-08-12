import asyncio
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import config
from erp.client import auto_login, fetch_timetable


SESSION_PATH: Path = config.SESSION_FILE


def _normalize_cookies(cookies) -> dict:
    """
    Convert cookies to a {name: value} dict.
    Supports both the old Playwright list format and the new dict format.
    """
    if isinstance(cookies, dict):
        return dict(cookies)
    if isinstance(cookies, list):
        return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}
    return {}


def save_session(cookies: dict, csrf_token: Optional[str] = None) -> None:
    payload = {
        "cookies": _normalize_cookies(cookies),
        "csrf_token": csrf_token,
    }
    SESSION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_session() -> dict:
    if not SESSION_PATH.exists():
        return {}
    data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    if "cookies" in data:
        data["cookies"] = _normalize_cookies(data["cookies"])
    return data


def session_to_cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def session_to_requests_cookies(cookies: dict) -> dict:
    return dict(cookies)


def _cookies_for_request(cookies: dict) -> dict:
    """
    The ERP expects _csrf and kl_erp_device_id cookie values to be URL-decoded
    when sent back in subsequent requests. This matches the reference backend.
    """
    result = dict(cookies)
    for key in ("_csrf", "kl_erp_device_id"):
        if key in result:
            result[key] = unquote(result[key])
    return result


async def perform_login(username: str, password: str, max_retries: int = 2) -> dict:
    """
    Log in to the ERP automatically using the local CAPTCHA solver.
    Retries a few times because CAPTCHA recognition is not always perfect.
    Saves and returns the authenticated session.
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            result = await auto_login(username, password)
            cookies = result["cookies"]
            csrf_token = result.get("csrf_token")
            save_session(cookies, csrf_token)
            return {"cookies": cookies, "csrf_token": csrf_token}
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                await asyncio.sleep(0.5)

    raise RuntimeError(f"Login failed after {max_retries} attempts. Last error: {last_error}")


async def fetch_timetable_html() -> str:
    """
    Fetch the latest timetable HTML using the stored session.
    Raises an exception if the session is missing or expired.
    """
    session = load_session()
    if not session or not session.get("cookies"):
        raise RuntimeError("No active ERP session. Please run /login first.")

    cookies = session.get("cookies", {})
    csrf_token = session.get("csrf_token", "")

    # Extract academic year and semester from the configured timetable URL.
    # Defaults are 29 and 1 if parsing fails.
    academic_year_code = "29"
    semester_id = "1"
    url = config.ERP_TIMETABLE_URL
    if "academicyear%5D=" in url:
        match = re.search(r"academicyear%5D=(\d+)", url)
        if match:
            academic_year_code = match.group(1)
    if "semesterid%5D=" in url:
        match = re.search(r"semesterid%5D=(\d+)", url)
        if match:
            semester_id = match.group(1)

    html = await fetch_timetable(_cookies_for_request(cookies), csrf_token, academic_year_code, semester_id)

    if not html.strip():
        raise RuntimeError("ERP returned an empty timetable response.")

    lower_html = html.lower()
    if "loginform" in lower_html or 'name="login-form"' in lower_html or "captcha" in lower_html:
        raise RuntimeError("ERP returned the login page. Session expired. Please run /login again.")

    return html
