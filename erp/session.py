import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import config
import storage
from erp.client import auto_login, fetch_timetable, reset_user_client

logger = logging.getLogger(__name__)


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


def save_session(cookies: dict, csrf_token: Optional[str] = None, user_id: Optional[int] = None) -> None:
    """Save session cookies strictly per-user in storage."""
    norm_cookies = _normalize_cookies(cookies)
    if user_id is not None:
        storage.save_user_session(user_id, norm_cookies, csrf_token)


def load_session(user_id: Optional[int] = None) -> dict:
    """Load active session strictly for the specified user."""
    if user_id is not None:
        sess = storage.load_user_session(user_id)
        if sess and sess.get("cookies"):
            sess["cookies"] = _normalize_cookies(sess["cookies"])
            return sess
    return {}


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


async def perform_login(
    username: str,
    password: str,
    user_id: Optional[int] = None,
    max_retries: Optional[int] = None,
) -> dict:
    """
    Log in to the ERP automatically using the local CAPTCHA solver.
    Retries multiple times because CAPTCHA recognition is not always 100% on first try.
    Uses gentle pacing and randomized backoff to avoid flooding ERP servers.
    Saves and returns the authenticated session for the given user.
    """
    if user_id is not None:
        reset_user_client(user_id)

    retries = max_retries if max_retries is not None else config.LOGIN_MAX_RETRIES
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            result = await auto_login(username, password, user_id=user_id)
            cookies = result["cookies"]
            csrf_token = result.get("csrf_token")
            save_session(cookies, csrf_token, user_id=user_id)
            return {"cookies": cookies, "csrf_token": csrf_token}
        except Exception as exc:
            last_error = exc
            err_str = str(exc).lower()
            if "429" in err_str or "too many requests" in err_str:
                logger.error("ERP rate limit (429) hit during login for user %s", user_id or "default")
                raise RuntimeError("ERP server is temporarily rate-limiting requests (429 Too Many Requests). Please wait 2-3 minutes before trying /login again.")

            if attempt < retries:
                # Never rush: space out attempts with 2s-4s gentle backoff & jitter
                delay = 2.0 + (attempt * 0.75) + random.uniform(0.5, 1.5)
                logger.warning(
                    "ERP login attempt %d/%d for user %s failed: %s. Pausing %.2fs before retrying...",
                    attempt,
                    retries,
                    user_id or "default",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    raise RuntimeError(f"Login failed after {retries} attempts. Last error: {last_error}")



async def _do_fetch_timetable(
    cookies: dict,
    csrf_token: Optional[str] = None,
    user_id: Optional[int] = None,
    priority: int = 1,
    metric: Optional[object] = None,
) -> str:
    """Internal helper to execute the timetable HTTP request."""
    academic_year_code = config.ERP_ACADEMIC_YEAR or ""
    semester_id = config.ERP_SEMESTER_ID or ""
    url = config.ERP_TIMETABLE_URL
    if not config.ERP_ACADEMIC_YEAR and "academicyear%5D=" in url:
        match = re.search(r"academicyear%5D=(\d+)", url)
        if match:
            academic_year_code = match.group(1)
    if not config.ERP_SEMESTER_ID and "semesterid%5D=" in url:
        match = re.search(r"semesterid%5D=(\d+)", url)
        if match:
            semester_id = match.group(1)

    html = await fetch_timetable(
        _cookies_for_request(cookies),
        csrf_token or "",
        academic_year_code,
        semester_id,
        user_id=user_id,
        priority=priority,
        metric=metric,
    )

    if not html.strip():
        raise RuntimeError("ERP returned an empty timetable response.")

    lower_html = html.lower()
    if "loginform" in lower_html or 'name="login-form"' in lower_html or "captcha" in lower_html:
        raise RuntimeError("ERP returned the login page. Session expired.")

    return html


async def fetch_timetable_html(
    user_id: Optional[int] = None,
    cookies: Optional[dict] = None,
    csrf_token: Optional[str] = None,
    priority: int = 1,
    auto_relogin: bool = True,
    metric: Optional[object] = None,
) -> str:
    """
    Fetch the latest live timetable HTML from ERP using warm session pooling.
    Guarantees 100% real-time data directly from ERP.
    If session is missing or expired, transparently auto re-logs in and retries once.
    """
    # 1. If explicit cookies provided (e.g. friend lookup)
    if cookies is not None:
        return await _do_fetch_timetable(
            cookies, csrf_token, user_id=user_id, priority=priority, metric=metric
        )

    if user_id is None:
        raise ValueError("user_id or cookies must be provided to fetch_timetable_html")

    uid = int(user_id)

    # 2. Fast Path: Attempt 1-hop fetch with active in-memory / MongoDB session
    session = load_session(user_id=uid)
    if session and session.get("cookies"):
        try:
            html = await _do_fetch_timetable(
                session["cookies"],
                session.get("csrf_token", ""),
                user_id=uid,
                priority=priority,
                metric=metric,
            )
            if metric and hasattr(metric, "is_warm"):
                metric.is_warm = True
            return html
        except Exception as exc:
            logger.info("Direct session fetch failed for user %s: %s. Attempting transparent re-login...", uid, exc)

    # 3. Session missing or expired -> Auto re-login if enabled
    if not auto_relogin:
        raise RuntimeError("No active ERP session. Please log in using /login first.")

    import credentials
    creds = credentials.load_user_credentials(uid)
    if not creds or not creds.get("username") or not creds.get("password"):
        raise RuntimeError("No active ERP session or saved credentials. Please run /login first.")

    t_login = time.perf_counter()
    login_res = await perform_login(creds["username"], creds["password"], user_id=uid)
    if metric:
        if hasattr(metric, "is_warm"):
            metric.is_warm = False
        if hasattr(metric, "login_ms"):
            metric.login_ms = (time.perf_counter() - t_login) * 1000.0

    new_cookies = login_res["cookies"]
    new_csrf = login_res.get("csrf_token", "")

    # 4. Retry timetable fetch with newly refreshed session
    return await _do_fetch_timetable(
        new_cookies,
        new_csrf,
        user_id=uid,
        priority=priority,
        metric=metric,
    )


async def warm_active_sessions() -> int:
    """
    Background session-warmer task:
    Keeps authenticated sessions warm in RAM for active users with stored credentials,
    preventing cold-start latency when users execute interactive commands.
    """
    import credentials
    from datetime import datetime, timezone

    morning_users = storage.get_all_morning_users()
    warmed_count = 0
    now = time.time()

    for uid in morning_users:
        try:
            sess = load_session(user_id=uid)
            creds = credentials.load_user_credentials(uid)
            if not creds or not creds.get("username") or not creds.get("password"):
                continue

            needs_warm = False
            if not sess or not sess.get("cookies"):
                needs_warm = True
            else:
                updated_at_str = sess.get("updated_at")
                if updated_at_str:
                    try:
                        dt = datetime.fromisoformat(updated_at_str)
                        if (now - dt.timestamp()) > 1500:  # Older than 25 minutes
                            needs_warm = True
                    except Exception:
                        needs_warm = True

            if needs_warm:
                logger.info("Session-warmer refreshing session for user %s", uid)
                await perform_login(creds["username"], creds["password"], user_id=uid, max_retries=2)
                warmed_count += 1
        except Exception as exc:
            logger.debug("Session warmer exception for user %s: %s", uid, exc)

    return warmed_count


