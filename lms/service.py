import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = timezone.utc

import credentials
import storage
from lms.client import LMSClient, LMS_BASE_URL

logger = logging.getLogger(__name__)


def clean_course_name(name: str) -> str:
    """
    Format long, uppercase university course titles into clean, readable titles.
    Removes clutter like '2026-2027 ODD SEMESTER' and formats casing.
    """
    if not name:
        return "Subject"

    cleaned = re.sub(r"\b\d{4}-\d{4}\s+(?:ODD|EVEN)\s+SEMESTER\b", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:ODD|EVEN)\s+SEMESTER\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:")

    if cleaned.isupper():
        small_words = {"and", "of", "in", "for", "to", "on", "with", "at", "by"}
        words = []
        for i, part in enumerate(cleaned.split()):
            if part.startswith("(") and part.endswith(")"):
                words.append(part)
            elif i > 0 and part.lower() in small_words:
                words.append(part.lower())
            else:
                words.append(part.capitalize())
        cleaned = " ".join(words)

    return cleaned or name


@dataclass
class LMSTask:
    id: str
    title: str
    course_name: str
    task_type: str
    due_timestamp: int
    is_submitted: bool = False
    url: str = ""

    @property
    def due_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.due_timestamp, tz=IST)

    @property
    def due_date_str(self) -> str:
        if self.due_timestamp <= 0:
            return "No due date"
        dt = self.due_datetime
        return dt.strftime("%d %b %Y, %I:%M %p")

    @property
    def seconds_remaining(self) -> float:
        if self.due_timestamp <= 0:
            return float("inf")
        return self.due_timestamp - time.time()

    @property
    def time_left_str(self) -> str:
        secs = self.seconds_remaining
        if secs == float("inf"):
            return "Open"
        if secs < 0:
            past_secs = abs(secs)
            hours = int(past_secs // 3600)
            mins = int((past_secs % 3600) // 60)
            if hours > 0:
                return f"Closed {hours}h ago"
            return f"Closed {mins}m ago"

        hours = int(secs // 3600)
        mins = int((secs % 3600) // 60)
        days = hours // 24
        rem_hours = hours % 24

        if days > 0:
            return f"{days}d {rem_hours}h left"
        if hours > 0:
            return f"{hours}h {mins}m left"
        return f"{mins}m left"

    @property
    def urgency(self) -> str:
        secs = self.seconds_remaining
        if secs < 0:
            return "overdue"
        if secs <= 24 * 3600:
            return "critical"
        if secs <= 48 * 3600:
            return "approaching"
        return "upcoming"


class LMSService:
    """High-level service for handling user LMS authentication, tasks, and alerts."""

    _task_cache: dict[int, tuple[float, list[LMSTask]]] = {}
    _course_cache: dict[int, tuple[float, dict[int, str]]] = {}
    _user_name_cache: dict[int, str] = {}

    @classmethod
    def invalidate_user_cache(cls, user_id: int) -> None:
        """Clear in-memory task, course, and profile caches for a user."""
        cls._task_cache.pop(user_id, None)
        cls._course_cache.pop(user_id, None)
        cls._user_name_cache.pop(user_id, None)

    @staticmethod
    async def get_authenticated_client(user_id: int) -> tuple[LMSClient, str]:
        """
        Retrieve an authenticated LMSClient for a user.
        Tries existing token first; re-authenticates if needed.
        Returns (LMSClient, student_name).
        """
        token = credentials.load_user_lms_token(user_id)
        client = LMSClient(token=token)

        if token:
            fullname = LMSService._user_name_cache.get(user_id, "")
            if fullname:
                return client, fullname
            try:
                site_info = await client.get_site_info()
                fullname = site_info.get("fullname", "")
                LMSService._user_name_cache[user_id] = fullname
                return client, fullname
            except (PermissionError, Exception):
                logger.info("Cached LMS token invalid or expired for user %s. Re-authenticating.", user_id)

        # Token was invalid or not present; load stored credentials
        creds = credentials.load_user_lms_credentials(user_id)
        if not creds:
            raise ValueError("No LMS credentials found. Please use /lms_login first.")

        token = await client.get_moodle_token(creds["username"], creds["password"])
        credentials.save_user_lms_token(user_id, token)

        try:
            site_info = await client.get_site_info()
            fullname = site_info.get("fullname", "")
            LMSService._user_name_cache[user_id] = fullname
        except Exception:
            fullname = creds["username"]

        return client, fullname

    @staticmethod
    async def fetch_all_pending_tasks(user_id: int, force_refresh: bool = False) -> list[LMSTask]:
        """
        Ultra-fast fetching of pending HAs, ALMs, quizzes, and assignments.
        Utilizes HTTP connection pooling, parallel async gathers, and fast memory caching.
        """
        now = time.time()
        if not force_refresh and user_id in LMSService._task_cache:
            cache_time, cached_tasks = LMSService._task_cache[user_id]
            if now - cache_time < 120:  # 2-minute fast TTL
                return cached_tasks

        client, _ = await LMSService.get_authenticated_client(user_id)

        # 1. Course mapping with 12-hour memory cache
        if user_id in LMSService._course_cache and (now - LMSService._course_cache[user_id][0] < 43200):
            course_map = LMSService._course_cache[user_id][1]
        else:
            site_info = await client.get_site_info()
            moodle_user_id = site_info.get("userid")
            courses = await client.get_enrolled_courses(moodle_user_id) if moodle_user_id else []
            course_map = {c["id"]: c.get("fullname", c.get("shortname", "Subject")) for c in courses}
            LMSService._course_cache[user_id] = (now, course_map)

        course_ids = list(course_map.keys())

        # 2. Concurrently fetch Calendar Action Events and Assignments
        async def fetch_action_events():
            try:
                return await client.get_action_events(timesortfrom=int(time.time() - 3600 * 12))
            except Exception as exc:
                logger.warning("Error fetching action events: %s", exc)
                return []

        async def fetch_assignments():
            if not course_ids:
                return []
            try:
                return await client.get_assignments(course_ids=course_ids)
            except Exception as exc:
                logger.warning("Error fetching assignments: %s", exc)
                return []

        action_events, assign_courses = await asyncio.gather(
            fetch_action_events(),
            fetch_assignments(),
        )

        tasks: dict[str, LMSTask] = {}

        # 3. Process Calendar Action Events
        for ev in action_events:
            event_id = str(ev.get("id"))
            instance_id = ev.get("instance")
            mod_name = ev.get("modulename", "").lower()
            raw_name = ev.get("name", "Assignment")
            clean_title = re.sub(r"\s+is\s+due$", "", raw_name, flags=re.IGNORECASE).strip()

            timesort = ev.get("timesort", 0)
            course_id = ev.get("course", {}).get("id") if isinstance(ev.get("course"), dict) else ev.get("course")
            raw_c_name = course_map.get(course_id, ev.get("course", {}).get("fullname", "Subject"))
            c_name = clean_course_name(raw_c_name)
            action_url = ev.get("action", {}).get("url", ev.get("url", ""))

            action_needed = ev.get("action", {}).get("actionable", True)
            if not action_needed:
                continue

            lower_title = clean_title.lower()
            if "alm" in lower_title:
                task_type = "ALM"
            elif "ha" in lower_title or "assignment" in lower_title:
                task_type = "Home Assignment"
            elif "quiz" in mod_name or "quiz" in lower_title:
                task_type = "Quiz"
            else:
                task_type = "Assignment"

            task_key = f"{mod_name}_{instance_id}" if instance_id else f"{mod_name}_{event_id}"
            tasks[task_key] = LMSTask(
                id=task_key,
                title=clean_title,
                course_name=c_name,
                task_type=task_type,
                due_timestamp=timesort,
                is_submitted=False,
                url=action_url,
            )

        # 4. Check Assignment Submission Statuses Concurrently
        assign_candidates = []
        for ac in assign_courses:
            raw_c_name = course_map.get(ac.get("id"), "Subject")
            c_name = clean_course_name(raw_c_name)
            for assign in ac.get("assignments", []):
                duedate = assign.get("duedate", 0)
                # Skip assignments closed more than 12 hours ago or distant future (> 45 days)
                if duedate > 0 and (now - duedate > 43200 or duedate - now > 45 * 86400):
                    continue
                assign_candidates.append((assign, c_name))

        async def check_status(item):
            assign, c_name = item
            assign_id = assign.get("id")
            try:
                status_info = await client.get_assignment_submission_status(assign_id)
                sub = status_info.get("lastattempt", {}).get("submission", {})
                sub_status = sub.get("status", "new").lower()
                return assign, c_name, sub_status
            except Exception:
                return assign, c_name, "unknown"

        if assign_candidates:
            sub_results = await asyncio.gather(*(check_status(item) for item in assign_candidates))
            for assign, c_name, sub_status in sub_results:
                assign_id = assign.get("id")
                if sub_status in {"submitted", "graded"}:
                    tasks.pop(f"assign_{assign_id}", None)
                    continue

                name = assign.get("name", "Home Assignment")
                duedate = assign.get("duedate", 0)
                cmid = assign.get("cmid")
                task_key = f"assign_{assign_id}"
                task_url = f"{LMS_BASE_URL}/mod/assign/view.php?id={cmid}" if cmid else ""

                lower_name = name.lower()
                if "ha" in lower_name or "home assignment" in lower_name:
                    t_type = "Home Assignment"
                elif "alm" in lower_name:
                    t_type = "ALM"
                elif "project" in lower_name:
                    t_type = "Project"
                else:
                    t_type = "Assignment"

                tasks[task_key] = LMSTask(
                    id=task_key,
                    title=name,
                    course_name=c_name,
                    task_type=t_type,
                    due_timestamp=duedate,
                    is_submitted=False,
                    url=task_url or tasks.get(task_key, LMSTask("", "", "", "", 0)).url,
                )

        # 5. Strict Deduplication
        deduped: dict[str, LMSTask] = {}
        for t in tasks.values():
            norm_c = re.sub(r"[^a-zA-Z0-9]", "", t.course_name).lower()
            norm_t = re.sub(r"[^a-zA-Z0-9]", "", t.title).lower()
            norm_key = f"{norm_c}_{norm_t}"

            if norm_key in deduped:
                existing = deduped[norm_key]
                if not existing.url and t.url:
                    deduped[norm_key] = t
            else:
                deduped[norm_key] = t

        # Sort tasks by due date ascending
        sorted_tasks = sorted(
            deduped.values(),
            key=lambda t: t.due_timestamp if t.due_timestamp > 0 else float("inf"),
        )
        LMSService._task_cache[user_id] = (time.time(), sorted_tasks)
        return sorted_tasks

    @staticmethod
    def format_tasks_telegram_message(tasks: list[LMSTask], student_name: str = "") -> str:
        """Format pending tasks grouped by subject with clean divider lines between tasks and subjects."""
        greeting = f"<b>{student_name}</b>, here" if student_name else "Here"

        if not tasks:
            return (
                "<b>All Caught Up</b>\n\n"
                f"{greeting} are no pending HAs, ALMs, or quizzes right now.\n"
                "All your university submissions are up to date."
            )

        from collections import defaultdict
        grouped = defaultdict(list)
        for t in tasks:
            grouped[t.course_name].append(t)

        # Sort courses by the earliest task due timestamp (urgent subjects first)
        sorted_courses = sorted(
            grouped.keys(),
            key=lambda c: min(t.due_timestamp for t in grouped[c] if t.due_timestamp > 0)
            if any(t.due_timestamp > 0 for t in grouped[c])
            else float("inf"),
        )

        subject_cards = []
        for course in sorted_courses:
            c_tasks = grouped[course]
            card_lines = [f"<b>{course}</b>"]

            for t in c_tasks:
                link_title = f"<a href=\"{t.url}\">{t.title}</a>" if t.url else t.title
                type_label = f" ({t.task_type})" if (t.task_type and t.task_type.lower() not in t.title.lower()) else ""

                if t.urgency == "overdue":
                    card_lines.append(f"• {link_title}{type_label}\n  <i>{t.time_left_str}</i>")
                else:
                    card_lines.append(f"• {link_title}{type_label}\n  Due: {t.due_date_str} · <b>{t.time_left_str}</b>")

            card_content = "\n".join(card_lines)
            subject_cards.append(f"<blockquote>{card_content}</blockquote>")

        cards_text = "\n".join(subject_cards)
        return (
            f"<b>KL LMS Pending Tasks ({len(tasks)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{cards_text}\n\n"
            "<i>Click any task name to open the LMS submission page.</i>"
        )

    @staticmethod
    def format_new_task_alert(task: LMSTask) -> str:
        """Professional alert when a new assignment is posted with blockquote card styling."""
        c_name = clean_course_name(task.course_name)
        link_title = f"<a href=\"{task.url}\">{task.title}</a>" if task.url else task.title
        type_label = f" ({task.task_type})" if (task.task_type and task.task_type.lower() not in task.title.lower()) else ""

        card_content = (
            f"<b>{c_name}</b>\n"
            f"• {link_title}{type_label}\n"
            f"  Due: {task.due_date_str} · <b>{task.time_left_str}</b>"
        )
        return (
            "📢 <b>NEW TASK ASSIGNED!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{card_content}</blockquote>\n\n"
            "<i>Complete it early to avoid last-minute portal rush.</i>"
        )

    @staticmethod
    def format_2day_reminder_alert(task: LMSTask) -> str:
        """Professional 48-hour deadline reminder with blockquote card styling."""
        c_name = clean_course_name(task.course_name)
        link_title = f"<a href=\"{task.url}\">{task.title}</a>" if task.url else task.title
        type_label = f" ({task.task_type})" if (task.task_type and task.task_type.lower() not in task.title.lower()) else ""

        card_content = (
            f"<b>{c_name}</b>\n"
            f"• {link_title}{type_label}\n"
            f"  Due: {task.due_date_str} · <b>{task.time_left_str}</b>"
        )
        return (
            "📢 <b>DEADLINE REMINDER (2 DAYS LEFT)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{card_content}</blockquote>\n\n"
            "<i>Click the task name to open the LMS submission page.</i>"
        )

    @staticmethod
    def format_urgent_reminder_alert(task: LMSTask) -> str:
        """Professional same-day closing alert with blockquote card styling."""
        c_name = clean_course_name(task.course_name)
        link_title = f"<a href=\"{task.url}\">{task.title}</a>" if task.url else task.title
        type_label = f" ({task.task_type})" if (task.task_type and task.task_type.lower() not in task.title.lower()) else ""

        card_content = (
            f"<b>{c_name}</b>\n"
            f"• {link_title}{type_label}\n"
            f"  Closes: {task.due_date_str} · <b>{task.time_left_str}</b>"
        )
        return (
            "📢 <b>URGENT: DUE TODAY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>{card_content}</blockquote>\n\n"
            "<i>Click the task name to submit on LMS now.</i>"
        )
