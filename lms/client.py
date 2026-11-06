import logging
from typing import Any, Optional
import httpx
from bs4 import BeautifulSoup
from security import backend_throttle

logger = logging.getLogger(__name__)

LMS_BASE_URL = "https://lms.kluniversity.in"
TOKEN_ENDPOINT = f"{LMS_BASE_URL}/login/token.php"
WS_ENDPOINT = f"{LMS_BASE_URL}/webservice/rest/server.php"
LOGIN_PAGE_URL = f"{LMS_BASE_URL}/login/index.php"


class LMSClient:
    """
    Asynchronous client for KL University Moodle LMS.
    Supports both Moodle Mobile Web Services REST API (primary)
    and session-based web scraping (fallback).
    """

    _shared_client: Optional[httpx.AsyncClient] = None

    @classmethod
    def get_shared_client(cls, timeout: float = 20.0) -> httpx.AsyncClient:
        if cls._shared_client is None or cls._shared_client.is_closed:
            cls._shared_client = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(max_keepalive_connections=30, max_connections=40, keepalive_expiry=120.0),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/html, */*",
                },
            )
        return cls._shared_client

    def __init__(self, token: Optional[str] = None, timeout: float = 15.0):
        self.token = token
        self.timeout = timeout
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html, */*",
        }

    async def get_moodle_token(self, username: str, password: str) -> str:
        """
        Authenticate against Moodle Mobile Web Service endpoint.
        Returns API token if successful, or raises ValueError on invalid credentials.
        """
        client = self.get_shared_client(timeout=self.timeout)
        payload = {
            "username": username.strip(),
            "password": password.strip(),
            "service": "moodle_mobile_app",
        }
        try:
            async with backend_throttle.lms_semaphore:
                resp = await client.post(TOKEN_ENDPOINT, data=payload)
            data = resp.json()
        except httpx.RequestError as exc:
            logger.error("Network error requesting LMS token: %s", exc)
            raise ConnectionError("Could not connect to KL LMS. Please try again later.") from exc
        except Exception as exc:
            logger.error("Error parsing LMS token response: %s", exc)
            raise ValueError("Unexpected response from KL LMS.") from exc

        if "token" in data:
            self.token = data["token"]
            logger.info("Successfully obtained Moodle API token for user %s", username)
            return self.token

        error_code = data.get("errorcode", "")
        error_msg = data.get("error", "Login failed")
        if error_code in {"invalidlogin", "usernotfullysetup"} or "Invalid login" in error_msg:
            raise ValueError("Invalid LMS username or password. Please check your credentials.")
        
        logger.warning("Token acquisition error: %s (code: %s)", error_msg, error_code)
        raise ValueError(f"LMS Login error: {error_msg}")

    async def call_ws(self, wsfunction: str, params: Optional[dict[str, Any]] = None) -> Any:
        """
        Execute a Moodle REST Web Service call using persistent connection pooling.
        """
        if not self.token:
            raise ValueError("LMS token is missing. Please authenticate first.")

        query_params = {
            "wstoken": self.token,
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
        }
        if params:
            query_params.update(params)

        client = self.get_shared_client(timeout=self.timeout)
        try:
            async with backend_throttle.lms_semaphore:
                resp = await client.post(WS_ENDPOINT, params=query_params)
            data = resp.json()
        except httpx.RequestError as exc:
            logger.error("Network error executing WS %s: %s", wsfunction, exc)
            raise ConnectionError(f"Network error communicating with LMS ({wsfunction}).") from exc
        except Exception as exc:
            logger.error("Failed to decode JSON from WS %s: %s", wsfunction, exc)
            raise ValueError(f"Invalid response from LMS ({wsfunction}).") from exc

        # Check if Moodle returned an exception JSON
        if isinstance(data, dict) and "exception" in data:
            error_msg = data.get("message", "Moodle WS Error")
            logger.warning("Moodle WS exception in %s: %s", wsfunction, error_msg)
            if data.get("errorcode") in {"invalidtoken", "accessexception"}:
                raise PermissionError("LMS session expired. Please log in again.")
            raise RuntimeError(error_msg)

        return data

    async def get_site_info(self) -> dict:
        """Fetch basic user info (userid, fullname, sitename)."""
        return await self.call_ws("core_webservice_get_site_info")

    async def get_enrolled_courses(self, user_id: int) -> list[dict]:
        """Fetch all courses enrolled by the user."""
        try:
            courses = await self.call_ws("core_enrol_get_users_courses", {"userid": user_id})
            if isinstance(courses, list):
                return courses
        except Exception as exc:
            logger.warning("Error calling core_enrol_get_users_courses: %s", exc)
        return []

    async def get_assignments(self, course_ids: Optional[list[int]] = None) -> list[dict]:
        """
        Fetch assignments for specified course IDs.
        If course_ids is empty, fetches for all enrolled courses.
        """
        params = {}
        if course_ids:
            for idx, cid in enumerate(course_ids):
                params[f"courseids[{idx}]"] = cid

        try:
            res = await self.call_ws("mod_assign_get_assignments", params)
            if isinstance(res, dict) and "courses" in res:
                return res["courses"]
        except Exception as exc:
            logger.warning("Error calling mod_assign_get_assignments: %s", exc)
        return []

    async def get_assignment_submission_status(self, assign_id: int) -> dict:
        """
        Get student's submission status for an assignment.
        Returns status dict (e.g. status: 'submitted', 'new', 'draft').
        """
        try:
            res = await self.call_ws("mod_assign_get_submission_status", {"assignid": assign_id})
            if isinstance(res, dict):
                return res
        except Exception as exc:
            logger.debug("Error checking submission status for assignid %s: %s", assign_id, exc)
        return {}

    async def get_user_quizzes(self, course_ids: Optional[list[int]] = None) -> list[dict]:
        """Fetch quizzes (ALMs / Quizzes) for courses."""
        params = {}
        if course_ids:
            for idx, cid in enumerate(course_ids):
                params[f"courseids[{idx}]"] = cid

        try:
            res = await self.call_ws("mod_quiz_get_user_quizzes_by_courses", params)
            if isinstance(res, dict) and "quizzes" in res:
                return res["quizzes"]
        except Exception as exc:
            logger.debug("Error calling mod_quiz_get_user_quizzes_by_courses: %s", exc)
        return []

    async def get_action_events(self, timesortfrom: int = 0) -> list[dict]:
        """
        Fetch calendar action events (upcoming assignments & quizzes needing action).
        """
        try:
            res = await self.call_ws(
                "core_calendar_get_action_events_by_timesort",
                {"timesortfrom": timesortfrom, "limitnum": 40},
            )
            if isinstance(res, dict) and "events" in res:
                return res["events"]
        except Exception as exc:
            logger.warning("Error calling core_calendar_get_action_events_by_timesort: %s", exc)
        return []

    # -------------------------------------------------------------------------
    # Web Session Fallback (if token API has restricted access for a student)
    # -------------------------------------------------------------------------

    async def login_web_session(self, username: str, password: str) -> httpx.AsyncClient:
        """
        Web session fallback: logs in via login/index.php and returns an authenticated client.
        """
        client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers=self.headers)
        try:
            login_page = await client.get(LOGIN_PAGE_URL)
            soup = BeautifulSoup(login_page.text, "html.parser")
            token_input = soup.find("input", {"name": "logintoken"})
            logintoken = token_input.get("value") if token_input else ""

            post_resp = await client.post(
                LOGIN_PAGE_URL,
                data={
                    "username": username.strip(),
                    "password": password.strip(),
                    "logintoken": logintoken,
                },
            )

            if "Invalid login" in post_resp.text:
                await client.aclose()
                raise ValueError("Invalid LMS username or password.")

            return client
        except Exception as exc:
            await client.aclose()
            logger.error("Web session login failed: %s", exc)
            raise
