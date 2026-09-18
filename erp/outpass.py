import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
import httpx

import config
from credentials import load_user_credentials
from erp.client import _get_headers, get_erp_client_for_user
from erp.session import _cookies_for_request, load_session, perform_login
from security import backend_throttle

logger = logging.getLogger(__name__)

URL_OUTPASS_LIST = f"{config.ERP_BASE_URL}/index.php?r=hostel%2Fhostel-student-outpass-requests-info%2Fmy_outpass_list"
URL_APPLY_LOCAL = f"{config.ERP_BASE_URL}/index.php?r=hostel%2Fhostel-student-outpass-requests-info%2Fapply_outpass"
URL_APPLY_HOME = f"{config.ERP_BASE_URL}/index.php?r=hostel%2Fhostel-student-outpass-requests-info%2Fapply_outpass2"
URL_GET_INTIME_JAX = f"{config.ERP_BASE_URL}/index.php?r=hostel%2Fhostel-student-outpass-requests-info%2Fget_intime_jax"


async def _get_authenticated_client(user_id: Optional[int] = None) -> Tuple[httpx.AsyncClient, dict, str, Optional[str]]:
    """
    Ensure active session exists and return (client, cookies, csrf_token, preloaded_html).
    Uses pooled persistent connections and reuses the liveness verification response
    to eliminate duplicate network requests.
    """
    sess = load_session(user_id=user_id)
    cookies = sess.get("cookies", {}) if sess else {}
    csrf_token = sess.get("csrf_token", "") if sess else ""

    headers = _get_headers()
    client = get_erp_client_for_user(user_id=user_id, timeout=30.0)

    req_cookies = _cookies_for_request(cookies) if cookies else {}
    preloaded_html: Optional[str] = None

    # Verify session liveness with outpass list check
    test_resp = await client.get(URL_OUTPASS_LIST, cookies=req_cookies, headers=headers, follow_redirects=True)
    if "site%2Flogin" in str(test_resp.url) or "site/login" in str(test_resp.url) or "LoginForm" in test_resp.text:
        logger.info("Session expired, performing re-login for outpass module...")
        creds = load_user_credentials(user_id) if user_id else None
        if not creds or not creds.get("username") or not creds.get("password"):
            raise RuntimeError("No active ERP session or stored credentials. Please log in with /login first.")

        login_res = await perform_login(creds["username"], creds["password"], user_id=user_id)
        cookies = login_res["cookies"]
        csrf_token = login_res["csrf_token"]
        req_cookies = _cookies_for_request(cookies)

        # Fetch outpass list once with the newly authenticated session
        fresh_resp = await client.get(URL_OUTPASS_LIST, cookies=req_cookies, headers=headers, follow_redirects=True)
        preloaded_html = fresh_resp.text
    else:
        # Re-use the already fetched HTML directly — zero extra network round-trip!
        preloaded_html = test_resp.text

    return client, req_cookies, csrf_token, preloaded_html


async def fetch_outpass_history(user_id: Optional[int] = None) -> List[Dict]:
    """
    Fetch the latest / active outpass request and return structured data list.
    Fast path uses the preloaded HTML from the authentication check.
    """
    async with backend_throttle.erp_semaphore:
        client, req_cookies, _, html = await _get_authenticated_client(user_id)
        if not html:
            headers = _get_headers()
            resp = await client.get(URL_OUTPASS_LIST, cookies=req_cookies, headers=headers, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return []

        outpasses = []
        rows = table.find_all("tr")
        for row in rows:
            cols = [td.text.strip() for td in row.find_all("td")]
            if len(cols) >= 12 and cols[1].isdigit():
                view_a = row.find("a", title="View")
                view_link = view_a.get("href") if view_a else ""

                outpasses.append({
                    "id": cols[1],
                    "student_id": cols[2],
                    "outing_date": cols[3],
                    "out_time": cols[4],
                    "in_time": cols[5],
                    "reason": cols[6],
                    "warden_name": cols[9],
                    "status": cols[10] or "Pending",
                    "approved_by": cols[11] or "N/A",
                    "cancellation_status": cols[12] if len(cols) > 12 else "",
                    "view_url": f"{config.ERP_BASE_URL}{view_link}" if view_link.startswith("/") else view_link,
                })
                break  # Focus strictly on the latest / active outpass

        return outpasses


async def get_available_in_times(out_time: str, user_id: Optional[int] = None) -> List[str]:
    """
    Call ERP AJAX endpoint to get allowed In-Times for a given Out-Time.
    Example out_time: '17:00:00' -> returns ['17:30:00', '18:00:00', ...]
    """
    headers = _get_headers()
    async with backend_throttle.erp_semaphore:
        client, req_cookies, _, _ = await _get_authenticated_client(user_id)
        try:
            # 1. Fetch form to get fresh CSRF and ensure cookies are synchronized
            form_resp = await client.get(URL_APPLY_LOCAL, cookies=req_cookies, headers=headers, follow_redirects=True)
            soup = BeautifulSoup(form_resp.text, "html.parser")
            csrf_input = soup.find("input", {"name": "_csrf"})
            fresh_csrf = csrf_input["value"] if csrf_input else ""

            for k, v in form_resp.cookies.items():
                req_cookies[k] = v

            ajax_headers = dict(headers)
            ajax_headers.update({
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": config.ERP_BASE_URL,
                "Referer": URL_APPLY_LOCAL,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            })

            payload = {
                "_csrf": fresh_csrf,
                "depdrop_parents[0]": out_time,
            }

            resp = await client.post(URL_GET_INTIME_JAX, data=payload, cookies=req_cookies, headers=ajax_headers)
            resp.raise_for_status()

            data = resp.json()
            output = data.get("output", [])
            return [item["id"] for item in output if "id" in item]
        except Exception as e:
            logger.error("Error fetching allowed in-times: %s", e)
            # Fallback generated standard half-hour slots after out_time
            try:
                h, m, _ = map(int, out_time.split(":"))
                slots = []
                for hour in range(h, 24):
                    for minute in (0, 30):
                        if hour == h and minute <= m:
                            continue
                        slots.append(f"{hour:02d}:{minute:02d}:00")
                return slots
            except Exception:
                return ["18:00:00", "18:30:00", "19:00:00", "19:30:00", "20:00:00", "20:30:00", "21:00:00", "21:30:00", "22:00:00"]


async def submit_local_outpass(
    date: str,
    out_time: str,
    in_time: str,
    reason: str,
    meals: Optional[Dict[str, str]] = None,
    user_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Submit Local Outing (Hours) Outpass Request to ERP.
    date: 'YYYY-MM-DD' (e.g. '2026-09-11')
    out_time: 'HH:MM:00' (e.g. '17:00:00')
    in_time: 'HH:MM:00' (e.g. '20:30:00')
    reason: reason string
    meals: {'breakfast': 'YES'/'NO', 'lunch': 'YES'/'NO', 'snack': 'YES'/'NO', 'dinner': 'YES'/'NO'}
    """
    if meals is None:
        meals = {"breakfast": "NO", "lunch": "NO", "snack": "NO", "dinner": "NO"}

    headers = _get_headers()
    async with backend_throttle.erp_semaphore:
        client, req_cookies, _, _ = await _get_authenticated_client(user_id)
        try:
            # Step 1: GET form page to extract dynamic profile ID and fresh CSRF
            form_resp = await client.get(URL_APPLY_LOCAL, cookies=req_cookies, headers=headers, follow_redirects=True)
            soup = BeautifulSoup(form_resp.text, "html.parser")

            csrf_input = soup.find("input", {"name": "_csrf"})
            csrf_token = csrf_input["value"] if csrf_input else ""

            profile_input = soup.find("input", {"name": "HostelStudentOutpassRequestsInfo[hostotpiid_profileid]"})
            profile_id = profile_input["value"] if profile_input else ""

            if not csrf_token:
                return False, "Failed to obtain CSRF token from ERP."
            if not profile_id:
                return False, "Hostel profile ID not found. Ensure you are enrolled in hostel management."

            for k, v in form_resp.cookies.items():
                req_cookies[k] = v

            # Step 2: POST form data
            post_headers = dict(headers)
            post_headers.update({
                "Origin": config.ERP_BASE_URL,
                "Referer": URL_APPLY_LOCAL,
                "Content-Type": "application/x-www-form-urlencoded",
            })

            payload = {
                "_csrf": csrf_token,
                "HostelStudentOutpassRequestsInfo[hostotpiid_profileid]": profile_id,
                "HostelStudentOutpassRequestsInfo[hostotpiid_date]": date,
                "HostelStudentOutpassRequestsInfo[hostotpiid_out_time]": out_time,
                "HostelStudentOutpassRequestsInfo[hostotpiid_in_time]": in_time,
                "HostelStudentOutpassRequestsInfo[hostotpiid_otpass_reason]": reason,
                "HostelStudentOutpassRequestsInfo[hostotpiid_brekfast_required]": meals.get("breakfast", "NO").upper(),
                "HostelStudentOutpassRequestsInfo[hostotpiid_lunch_required]": meals.get("lunch", "NO").upper(),
                "HostelStudentOutpassRequestsInfo[hostotpiid_snack_required]": meals.get("snack", "NO").upper(),
                "HostelStudentOutpassRequestsInfo[hostotpiid_dinner_required]": meals.get("dinner", "NO").upper(),
            }

            resp = await client.post(URL_APPLY_LOCAL, data=payload, cookies=req_cookies, headers=post_headers)
            
            # Check for successful redirection or presence in my_outpass_list
            resp_text = resp.text
            if "has-error" in resp_text or "help-block-error" in resp_text:
                err_soup = BeautifulSoup(resp_text, "html.parser")
                err_block = err_soup.find("div", class_="help-block-error")
                err_msg = err_block.text.strip() if err_block else "ERP validation failed. Check your timings or details."
                return False, err_msg

            return True, "Local outpass applied successfully! Waiting for warden approval."
        except (httpx.TimeoutException, httpx.NetworkError) as timeout_exc:
            logger.warning("Network timeout during local outpass submission for user %s: %s. Performing post-timeout verification...", user_id, timeout_exc)
            try:
                history = await fetch_outpass_history(user_id=user_id)
                for h in history:
                    if h.get("outing_date") == date or date in h.get("out_time", ""):
                        return True, f"Local outpass verified on ERP (Status: {h.get('status', 'Pending')})! It was processed before the network timeout."
            except Exception as hist_err:
                logger.error("Failed to verify outpass history after timeout: %s", hist_err)
            return False, "ERP connection timed out during submission. Please check your outpass history before retrying."
        except Exception as e:
            logger.error("Failed to submit local outpass: %s", e)
            return False, str(e)


async def submit_home_outpass(
    out_datetime: str,
    in_datetime: str,
    reason: str,
    user_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Submit Home Outing (Days) Outpass Request to ERP.
    out_datetime: 'YYYY-MM-DD HH:MM:SS' (e.g. '2026-09-12 09:00:00')
    in_datetime: 'YYYY-MM-DD HH:MM:SS' (e.g. '2026-09-15 18:00:00')
    reason: reason string
    """
    headers = _get_headers()
    async with backend_throttle.erp_semaphore:
        client, req_cookies, _, _ = await _get_authenticated_client(user_id)
        try:
            # Step 1: GET form page
            form_resp = await client.get(URL_APPLY_HOME, cookies=req_cookies, headers=headers, follow_redirects=True)
            soup = BeautifulSoup(form_resp.text, "html.parser")

            csrf_input = soup.find("input", {"name": "_csrf"})
            csrf_token = csrf_input["value"] if csrf_input else ""

            profile_input = soup.find("input", {"name": "HostelStudentOutpassRequestsInfo[hostotpiid_profileid]"})
            profile_id = profile_input["value"] if profile_input else ""

            if not csrf_token:
                return False, "Failed to obtain CSRF token from ERP."
            if not profile_id:
                return False, "Hostel profile ID not found. Ensure you are enrolled in hostel management."

            for k, v in form_resp.cookies.items():
                req_cookies[k] = v

            post_headers = dict(headers)
            post_headers.update({
                "Origin": config.ERP_BASE_URL,
                "Referer": URL_APPLY_HOME,
                "Content-Type": "application/x-www-form-urlencoded",
            })

            payload = {
                "_csrf": csrf_token,
                "HostelStudentOutpassRequestsInfo[hostotpiid_profileid]": profile_id,
                "HostelStudentOutpassRequestsInfo[hostotpiid_out_time]": out_datetime,
                "HostelStudentOutpassRequestsInfo[hostotpiid_in_time]": in_datetime,
                "HostelStudentOutpassRequestsInfo[hostotpiid_otpass_reason]": reason,
            }

            resp = await client.post(URL_APPLY_HOME, data=payload, cookies=req_cookies, headers=post_headers)

            resp_text = resp.text
            if "has-error" in resp_text or "help-block-error" in resp_text:
                err_soup = BeautifulSoup(resp_text, "html.parser")
                err_block = err_soup.find("div", class_="help-block-error")
                err_msg = err_block.text.strip() if err_block else "ERP validation failed. Check dates or reason."
                return False, err_msg

            return True, "Home outpass applied successfully! Waiting for warden approval."
        except (httpx.TimeoutException, httpx.NetworkError) as timeout_exc:
            target_date = out_datetime.split()[0] if " " in out_datetime else out_datetime
            logger.warning("Network timeout during home outpass submission for user %s: %s. Performing post-timeout verification...", user_id, timeout_exc)
            try:
                history = await fetch_outpass_history(user_id=user_id)
                for h in history:
                    if h.get("outing_date") == target_date or target_date in h.get("out_time", ""):
                        return True, f"Home outpass verified on ERP (Status: {h.get('status', 'Pending')})! It was processed before the network timeout."
            except Exception as hist_err:
                logger.error("Failed to verify home outpass history after timeout: %s", hist_err)
            return False, "ERP connection timed out during submission. Please check your outpass history before retrying."
        except Exception as e:
            logger.error("Failed to submit home outpass: %s", e)
            return False, str(e)
