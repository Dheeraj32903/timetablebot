import json
import os
import time
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, Browser, Page

import config
from captcha_solver import solve_captcha_image


def _session_path() -> str:
    return config.SESSION_FILE


def save_session(cookies: list, csrf_token: Optional[str] = None) -> None:
    payload = {
        "cookies": cookies,
        "csrf_token": csrf_token,
    }
    with open(_session_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_session() -> dict:
    if not os.path.exists(_session_path()):
        return {}
    with open(_session_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def session_to_cookie_header(cookies: list) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def session_to_requests_cookies(cookies: list) -> dict:
    return {c["name"]: c["value"] for c in cookies}


def extract_csrf_from_page(page: Page) -> Optional[str]:
    """Try common Yii CSRF token locations."""
    selectors = [
        'meta[name="csrf-token"]',
        'meta[name="_csrf"]',
        'input[name="_csrf"]',
    ]
    for selector in selectors:
        try:
            element = page.query_selector(selector)
            if element:
                token = element.get_attribute("content") or element.get_attribute("value")
                if token:
                    return token
        except Exception:
            continue

    # Fallback: read from window.yii if available.
    try:
        token = page.evaluate("() => window.yii && window.yii.getCsrfToken ? window.yii.getCsrfToken() : null")
        if token:
            return token
    except Exception:
        pass

    return None


def _fill_login_form(page: Page) -> None:
    """Fill username and password fields using common Yii selectors."""
    if not config.ERP_USERNAME or not config.ERP_PASSWORD:
        raise RuntimeError(
            "ERP_USERNAME and ERP_PASSWORD must be set in .env for automatic login."
        )

    filled_username = False
    for selector in ['input[name="LoginForm[username]"]', 'input#loginform-username', 'input[name="username"]']:
        try:
            page.fill(selector, config.ERP_USERNAME, timeout=3_000)
            filled_username = True
            break
        except Exception:
            continue
    if not filled_username:
        raise RuntimeError("Could not find the username input field on the login page.")

    filled_password = False
    for selector in ['input[name="LoginForm[password]"]', 'input#loginform-password', 'input[name="password"]']:
        try:
            page.fill(selector, config.ERP_PASSWORD, timeout=3_000)
            filled_password = True
            break
        except Exception:
            continue
    if not filled_password:
        raise RuntimeError("Could not find the password input field on the login page.")


def _find_captcha_image_src(page: Page) -> Optional[str]:
    """Find the CAPTCHA image URL on the login page."""
    selectors = [
        'img#loginform-captcha-image',
        'img.captcha',
        'img[src*="captcha"]',
        'img[data yii-captcha]',
    ]
    for selector in selectors:
        try:
            element = page.query_selector(selector)
            if element:
                src = element.get_attribute("src")
                if src:
                    return src
        except Exception:
            continue
    return None


def _fill_captcha_input(page: Page, captcha_text: str) -> bool:
    """Fill the CAPTCHA input field."""
    for selector in ['input[name="LoginForm[captcha]"]', 'input#loginform-captcha', 'input[name="captcha"]']:
        try:
            page.fill(selector, captcha_text, timeout=3_000)
            return True
        except Exception:
            continue
    return False


def _submit_login_form(page: Page) -> bool:
    """Click the login form submit button."""
    for selector in ['button[type="submit"]', 'input[type="submit"]', 'button[name="login-button"]', 'button:has-text("Login")']:
        try:
            page.click(selector, timeout=3_000)
            return True
        except Exception:
            continue
    return False


def _wait_for_login(page: Page, timeout_ms: int) -> None:
    """Wait until the browser navigates away from the login page."""
    try:
        page.wait_for_event(
            "framenavigated",
            predicate=lambda frame: frame == page.main_frame and "login" not in frame.url.lower(),
            timeout=timeout_ms,
        )
    except Exception as exc:
        raise RuntimeError("Timed out waiting for login. Did the CAPTCHA or credentials fail?") from exc


def perform_manual_login(timeout_ms: int = 300_000) -> dict:
    """
    Open the ERP login page in a visible browser.
    If CAPTCHA_SERVICE and CAPTCHA_API_KEY are configured, the CAPTCHA is solved automatically.
    Otherwise, the user completes the CAPTCHA and logs in manually.
    Once logged in, the authenticated cookies and CSRF token are captured and saved.
    """
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=config.ERP_USER_AGENT,
        )
        page: Page = context.new_page()

        try:
            page.goto(config.ERP_LOGIN_URL, wait_until="networkidle", timeout=60_000)

            _fill_login_form(page)

            auto_solve = config.CAPTCHA_SERVICE.lower().strip() == "2captcha" and config.CAPTCHA_API_KEY

            if auto_solve:
                captcha_src = _find_captcha_image_src(page)
                if not captcha_src:
                    raise RuntimeError("CAPTCHA auto-solve is enabled but no CAPTCHA image was found on the page.")

                # Make the URL absolute if it is relative.
                if captcha_src.startswith("/"):
                    captcha_url = config.ERP_BASE_URL + captcha_src
                else:
                    captcha_url = captcha_src

                cookies = session_to_requests_cookies(context.cookies())
                headers = {"User-Agent": config.ERP_USER_AGENT}
                captcha_text = solve_captcha_image(captcha_url, cookies, headers)

                if not _fill_captcha_input(page, captcha_text):
                    raise RuntimeError("Could not find the CAPTCHA input field.")

                if not _submit_login_form(page):
                    raise RuntimeError("Could not find the login submit button.")

                _wait_for_login(page, timeout_ms)
            else:
                # Manual mode: wait for the user to complete CAPTCHA and submit the form.
                _wait_for_login(page, timeout_ms)

            # Let the post-login page settle.
            page.wait_for_load_state("networkidle", timeout=30_000)

            # Extract CSRF token from the logged-in page.
            csrf_token = extract_csrf_from_page(page)

            cookies = context.cookies()
            save_session(cookies, csrf_token)
            return {"cookies": cookies, "csrf_token": csrf_token}
        finally:
            browser.close()


def fetch_timetable_html() -> str:
    """
    Fetch the latest timetable HTML using the stored session.
    Raises an exception if the session is missing or expired.
    """
    session = load_session()
    if not session or not session.get("cookies"):
        raise RuntimeError("No active ERP session. Please run /login first.")

    cookies = session.get("cookies", [])
    csrf_token = session.get("csrf_token")

    headers = {
        "User-Agent": config.ERP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8,te;q=0.7",
        "Referer": config.ERP_TIMETABLE_REFERER_URL,
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": "\"Android\"",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    # The timetable GET observed from the browser does not send X-CSRF-Token.
    # It is kept here only if your ERP variant requires it for other routes.
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token

    request_cookies = session_to_requests_cookies(cookies)

    response = requests.get(
        config.ERP_TIMETABLE_URL,
        headers=headers,
        cookies=request_cookies,
        timeout=60,
        allow_redirects=True,
    )

    # Yii commonly redirects to login when the session expires.
    if response.status_code in (302, 301) or "login" in response.url.lower():
        raise RuntimeError("ERP session expired. Please run /login again.")

    if response.status_code != 200:
        raise RuntimeError(f"ERP returned status {response.status_code}")

    html = response.text
    if not html.strip():
        raise RuntimeError("ERP returned an empty timetable response.")

    # Some ERPs render the login page with HTTP 200 when the session expires.
    lower_html = html.lower()
    if "loginform" in lower_html or 'name="login-form"' in lower_html or "captcha" in lower_html:
        raise RuntimeError("ERP returned the login page. Session expired. Please run /login again.")

    return html
