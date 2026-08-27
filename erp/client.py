import asyncio
import random
import re
import time
import urllib3
import warnings
from typing import Optional
from urllib.parse import urlparse

import httpx

import config
from erp.captcha_solver import solve_captcha

# We use verify=False because the reference backend does, and some university ERP
# certificates can cause issues. Suppress the noisy warning.
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


BASE_URL = config.ERP_BASE_URL
LOGIN_URL = f"{BASE_URL}/index.php?r=site%2Flogin"


def _get_headers() -> dict:
    """Return browser-like headers similar to the reference backend."""
    chrome_versions = ["124.0.0.0", "125.0.0.0", "126.0.0.0"]
    selected_version = random.choice(chrome_versions)
    major = selected_version.split(".")[0]

    return {
        "User-Agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{selected_version} Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _extract_csrf(html: str) -> str:
    """Extract the Yii CSRF token from HTML."""
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'<input[^>]+name="_csrf"[^>]+value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'<input[^>]+value="([^"]+)"[^>]+name="_csrf"', html)
    if m:
        return m.group(1)
    return ""


def _collect_cookies(response: httpx.Response, base: dict) -> dict:
    """Merge Set-Cookie headers into a cookie jar."""
    merged = dict(base)
    for header_val in response.headers.get_list("set-cookie"):
        part = header_val.split(";")[0].strip()
        if "=" in part:
            k, v = part.split("=", 1)
            merged[k.strip()] = v.strip()
    return merged


def _is_login_failed(response: httpx.Response) -> bool:
    """Detect whether the ERP returned the login page (login failed or expired)."""
    url_str = str(response.url)
    if "site%2Flogin" in url_str or "site/login" in url_str:
        return True
    text = response.text
    if "LoginForm[username]" in text or "LoginForm[password]" in text:
        return True
    if "<h4" in text and "Login" in text:
        return True
    return False


async def _follow_redirects(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    cookies: dict,
    timeout: int = 30,
    **kwargs,
) -> tuple[httpx.Response, dict]:
    """Follow redirects manually while collecting cookies."""
    current_url = url
    current_cookies = dict(cookies)
    max_redirects = 10

    for _ in range(max_redirects):
        if method == "POST":
            resp = await client.post(
                current_url,
                cookies=current_cookies,
                follow_redirects=False,
                timeout=timeout,
                **kwargs,
            )
        else:
            resp = await client.get(
                current_url,
                cookies=current_cookies,
                follow_redirects=False,
                timeout=timeout,
                **kwargs,
            )

        current_cookies = _collect_cookies(resp, current_cookies)

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if not location:
                break

            parsed = urlparse(current_url)
            if location.startswith("/"):
                location = f"{parsed.scheme}://{parsed.netloc}{location}"
            elif not location.startswith("http"):
                location = f"{BASE_URL}/{location.lstrip('/')}"

            # Fix possible http-over-https mangling.
            if location.startswith("http://newerp.kluniversity.in:443"):
                location = location.replace(
                    "http://newerp.kluniversity.in:443",
                    "https://newerp.kluniversity.in",
                )
            elif location.startswith("http://newerp.kluniversity.in"):
                location = location.replace(
                    "http://newerp.kluniversity.in",
                    "https://newerp.kluniversity.in",
                )

            current_url = location
            method = "GET"
            kwargs = {}
        else:
            return resp, current_cookies

    return resp, current_cookies


async def auto_login(username: str, password: str) -> dict:
    """
    Log in to the ERP automatically.
    Returns a dict with cookies and the logged-in page response.
    """
    headers = _get_headers()

    async with httpx.AsyncClient(
        verify=True,
        headers=headers,
        http2=False,
        follow_redirects=False,
        timeout=30.0,
    ) as client:
        # Step 1: GET login page to collect CSRF and initial cookies.
        res, cookies = await _follow_redirects(client, "GET", LOGIN_URL, {}, timeout=30)
        res.raise_for_status()

        csrf = _extract_csrf(res.text)
        if not csrf:
            raise RuntimeError("CSRF token not found on login page.")

        # Step 2: POST empty login form to trigger CAPTCHA image generation.
        await asyncio.sleep(random.uniform(0.1, 0.3))

        form_headers = dict(headers)
        form_headers["Origin"] = BASE_URL
        form_headers["Referer"] = LOGIN_URL
        form_headers["Sec-Fetch-Site"] = "same-origin"
        form_headers["Sec-Fetch-Mode"] = "cors"
        form_headers["Sec-Fetch-Dest"] = "empty"
        form_headers["Content-Type"] = "application/x-www-form-urlencoded"

        dummy_data = {
            "_csrf": csrf,
            "LoginForm[username]": "",
            "LoginForm[password]": "",
        }
        res_post, cookies = await _follow_redirects(
            client, "POST", LOGIN_URL, cookies, data=dummy_data, headers=form_headers, timeout=30
        )
        res_post.raise_for_status()

        # Step 3: Extract CAPTCHA image URL.
        captcha_match = re.search(r'src="([^"]*?r=site%2Fcaptcha[^"]*?)"', res_post.text)
        if not captcha_match:
            raise RuntimeError("CAPTCHA image locator missing from login page.")

        captcha_url = BASE_URL + captcha_match.group(1).replace("&amp;", "&")

        # Step 4: Download CAPTCHA image.
        image_headers = dict(headers)
        image_headers["Accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        image_headers["Sec-Fetch-Mode"] = "no-cors"
        image_headers["Sec-Fetch-Dest"] = "image"
        image_headers["Referer"] = LOGIN_URL

        captcha_response, cookies = await _follow_redirects(
            client, "GET", captcha_url, cookies, headers=image_headers, timeout=30
        )
        captcha_response.raise_for_status()

        # Step 5: Solve CAPTCHA locally.
        captcha_text = solve_captcha(captcha_response.content)
        if not captcha_text:
            raise RuntimeError("CAPTCHA solver returned empty text.")

        # Step 6: Submit real login credentials.
        payload = {
            "_csrf": csrf,
            "LoginForm[username]": username,
            "LoginForm[password]": password,
            "LoginForm[captcha]": captcha_text,
            "LoginForm[rememberMe]": "0",
            "LoginForm[qr_code]": "",
        }

        submit_headers = dict(headers)
        submit_headers["Origin"] = BASE_URL
        submit_headers["Referer"] = LOGIN_URL
        submit_headers["Sec-Fetch-Site"] = "same-origin"
        submit_headers["Sec-Fetch-Mode"] = "navigate"
        submit_headers["Sec-Fetch-Dest"] = "document"
        submit_headers["Sec-Fetch-User"] = "?1"
        submit_headers["Content-Type"] = "application/x-www-form-urlencoded"

        await asyncio.sleep(random.uniform(0.1, 0.3))

        response, final_cookies = await _follow_redirects(
            client, "POST", LOGIN_URL, cookies, data=payload, headers=submit_headers, timeout=30
        )
        response.raise_for_status()

        if _is_login_failed(response):
            if "incorrect" in response.text.lower() or "invalid" in response.text.lower():
                raise RuntimeError(
                    "ERP rejected the login. Check username, password, or CAPTCHA."
                )
            raise RuntimeError("ERP login failed (returned login page).")

        final_csrf = _extract_csrf(response.text) or csrf

        return {
            "cookies": final_cookies,
            "csrf_token": final_csrf,
            "response": response,
        }


async def fetch_timetable(
    cookies: dict,
    csrf_token: str,
    academic_year_code: str = "29",
    semester_id: str = "1",
) -> str:
    """
    Fetch the timetable HTML using the authenticated session.
    """
    headers = _get_headers()
    headers["Referer"] = (
        f"{BASE_URL}/index.php?r=timetables%2Funiversitymasteracademictimetableview%2Findexstudentindisearch"
    )
    headers["Sec-Fetch-Site"] = "same-origin"

    tt_url = (
        f"{BASE_URL}/index.php?r=timetables%2Funiversitymasteracademictimetableview%2Findividualstudenttimetableget"
        f"&UniversityMasterAcademicTimetableView%5Bacademicyear%5D={academic_year_code}"
        f"&UniversityMasterAcademicTimetableView%5Bsemesterid%5D={semester_id}"
    )

    async with httpx.AsyncClient(
        verify=True,
        headers=headers,
        http2=False,
        follow_redirects=True,
        timeout=35.0,
    ) as client:
        response = await client.get(tt_url, cookies=cookies, timeout=35.0)
        response.raise_for_status()

        if _is_login_failed(response):
            raise RuntimeError("ERP session expired or login required.")

        return response.text
