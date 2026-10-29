import asyncio
import logging
import os
import time
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import credentials
import erp.outpass as outpass_api
from erp.session import fetch_timetable_html, perform_login
from lms.service import LMSService, LMSTask
from parser.timetable import _ist_now, _today_day_name, parse_timetable_structured
import storage
from webapp.auth import AuthenticatedUser, get_current_user
from webapp.schemas import (
    AllowedInTimesResponse,
    ClassSlot,
    DashboardResponse,
    DaySchedule,
    HomeOutpassRequest,
    IntegrationStatus,
    LMSTaskItem,
    LMSTasksResponse,
    LocalOutpassRequest,
    NextClassInfo,
    OutpassHistoryResponse,
    OutpassRecord,
    OutpassSubmitResponse,
    TimetableResponse,
    UserProfileResponse,
)
from workload import workload_manager

logger = logging.getLogger(__name__)

app = FastAPI(
    title="KL University Student Companion API",
    description="Backend API for Telegram Mini App Student Dashboard",
    version="2.0.0",
)

# Enable CORS for Mini App webviews
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _parse_time_to_minutes(time_str: str) -> Optional[int]:
    """Convert '09:20 AM' or '01:50 PM' to minutes since midnight."""
    try:
        t = time_str.strip()
        parts = t.split()
        if len(parts) == 2:
            hm, ampm = parts
            h, m = map(int, hm.split(":"))
            if ampm.upper() == "PM" and h != 12:
                h += 12
            elif ampm.upper() == "AM" and h == 12:
                h = 0
            return h * 60 + m
    except Exception:
        pass
    return None


def _compute_next_class(classes: List[dict]) -> Optional[NextClassInfo]:
    """Find currently active or next upcoming class for today in IST."""
    if not classes:
        return None

    now_ist = _ist_now()
    now_min = now_ist.hour * 60 + now_ist.minute

    in_progress = None
    next_upcoming = None

    for c in classes:
        start_min = _parse_time_to_minutes(c.get("start_time", ""))
        end_min = _parse_time_to_minutes(c.get("end_time", ""))

        if start_min is None or end_min is None:
            continue

        if start_min <= now_min < end_min:
            rem = end_min - now_min
            in_progress = NextClassInfo(
                subject_title=c.get("subject_title", c.get("subject_code", "")),
                subject_code=c.get("subject_code", ""),
                class_type=c.get("class_type", "Class"),
                room=c.get("room", "TBA"),
                section=c.get("section", "—"),
                start_time=c.get("start_time", ""),
                end_time=c.get("end_time", ""),
                status="in_progress",
                time_badge=f"Ends in {rem}m",
            )
            break
        elif start_min > now_min:
            if next_upcoming is None:
                diff = start_min - now_min
                h = diff // 60
                m = diff % 60
                badge = f"Starts in {h}h {m}m" if h > 0 else f"Starts in {m}m"
                next_upcoming = NextClassInfo(
                    subject_title=c.get("subject_title", c.get("subject_code", "")),
                    subject_code=c.get("subject_code", ""),
                    class_type=c.get("class_type", "Class"),
                    room=c.get("room", "TBA"),
                    section=c.get("section", "—"),
                    start_time=c.get("start_time", ""),
                    end_time=c.get("end_time", ""),
                    status="upcoming",
                    time_badge=badge,
                )

    return in_progress or next_upcoming


async def _fetch_timetable_job(uid: int, metric=None) -> str:
    """Worker job handler for fetching live timetable through workload queues."""
    try:
        return await fetch_timetable_html(user_id=uid)
    except Exception:
        saved = credentials.load_user_credentials(uid)
        if not saved:
            raise RuntimeError("No active ERP credentials stored. Please connect ERP first.")
        await perform_login(saved["username"], saved["password"], user_id=uid)
        return await fetch_timetable_html(user_id=uid)


def _map_lms_task(t: LMSTask) -> LMSTaskItem:
    """Convert an internal LMSTask dataclass into an API schema."""
    if t.is_submitted:
        urg = "submitted"
    elif t.seconds_remaining <= 24 * 3600:
        urg = "urgent"
    elif t.seconds_remaining <= 48 * 3600:
        urg = "due_soon"
    else:
        urg = "upcoming"

    return LMSTaskItem(
        id=t.id,
        title=t.title,
        course_name=t.course_name,
        task_type=t.task_type,
        due_timestamp=t.due_timestamp,
        due_date_str=t.due_date_str,
        time_left_str=t.time_left_str,
        seconds_remaining=t.seconds_remaining,
        is_submitted=t.is_submitted,
        urgency=urg,
        url=t.url,
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
async def health_check():
    """Liveness health check endpoint for monitoring."""
    return {"status": "ok", "app": "klu-bot-miniapp", "timestamp": time.time()}


@app.get("/api/me", response_model=UserProfileResponse, tags=["Profile"])
async def get_my_profile(user: AuthenticatedUser = Depends(get_current_user)):
    """Return student profile, service connections, and morning alert preferences."""
    erp_creds = credentials.load_user_credentials(user.id)
    lms_creds = credentials.load_user_lms_credentials(user.id)
    morning_enabled = storage.is_user_morning_enabled(user.id)

    student_id = erp_creds.get("username") if erp_creds else None

    return UserProfileResponse(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        telegram_username=user.username,
        student_id=student_id,
        erp=IntegrationStatus(
            connected=bool(erp_creds),
            username=student_id,
            status_text="Connected" if erp_creds else "Not Connected",
            details="Active ERP Session" if erp_creds else "Log in via Telegram to link ERP",
        ),
        lms=IntegrationStatus(
            connected=bool(lms_creds),
            username=lms_creds.get("username") if lms_creds else None,
            status_text="Connected" if lms_creds else "Not Connected",
            details="Active Moodle Token" if lms_creds else "Connect via /lms_login",
        ),
        morning_alerts_enabled=morning_enabled,
        morning_alert_time="06:45 AM IST",
    )


@app.get("/api/timetable", response_model=TimetableResponse, tags=["Timetable"])
async def get_timetable(
    force_refresh: bool = Query(False, description="Bypass cache and fetch directly from ERP"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get structured daily and weekly timetable with class room and timing details."""
    today = _today_day_name()
    erp_creds = credentials.load_user_credentials(user.id)
    if not erp_creds:
        return TimetableResponse(
            current_day=today,
            has_timetable=False,
            is_live_synced=False,
            days=[],
            error_message="ERP account not connected. Please log in via Telegram first.",
        )

    html_content: Optional[str] = None
    is_live = False

    if not force_refresh:
        html_content = storage.load_morning_cache(user.id)

    if not html_content or force_refresh:
        try:
            html_content = await workload_manager.submit_job(
                workload_manager.timetable_queue,
                user.id,
                "fetch_timetable",
                "timetable",
                _fetch_timetable_job,
                user.id,
            )
            is_live = True
            storage.save_morning_cache(user.id, html_content)
        except Exception as exc:
            logger.error("Failed to fetch timetable for user %s: %s", user.id, exc)
            if not html_content:
                return TimetableResponse(
                    current_day=today,
                    has_timetable=False,
                    is_live_synced=False,
                    days=[],
                    error_message=f"Could not load timetable from ERP: {str(exc)}",
                )

    days_data = parse_timetable_structured(html_content)
    days = [DaySchedule(**d) for d in days_data]

    return TimetableResponse(
        current_day=today,
        has_timetable=len(days) > 0,
        is_live_synced=is_live,
        days=days,
        error_message=None if days else "No schedule rows could be extracted.",
    )


@app.post("/api/timetable/refresh", response_model=TimetableResponse, tags=["Timetable"])
async def refresh_timetable(user: AuthenticatedUser = Depends(get_current_user)):
    """Force immediate live ERP timetable sync."""
    return await get_timetable(force_refresh=True, user=user)


@app.get("/api/lms/tasks", response_model=LMSTasksResponse, tags=["LMS"])
async def get_lms_tasks(
    force_refresh: bool = Query(False, description="Force fresh fetch from Moodle REST API"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Retrieve all pending and submitted assignments, quizzes, and tasks from Moodle LMS."""
    lms_creds = credentials.load_user_lms_credentials(user.id)
    if not lms_creds:
        return LMSTasksResponse(
            is_connected=False,
            student_name=None,
            total_pending=0,
            total_submitted=0,
            urgent_count=0,
            tasks=[],
            error_message="LMS account not connected. Use /lms_login in Telegram to connect.",
        )

    try:
        raw_tasks = await LMSService.fetch_all_pending_tasks(user.id, force_refresh=force_refresh)
        _, student_name = await LMSService.get_authenticated_client(user.id)

        items = [_map_lms_task(t) for t in raw_tasks]
        pending = [it for it in items if not it.is_submitted]
        submitted = [it for it in items if it.is_submitted]
        urgent = [it for it in pending if it.urgency == "urgent"]

        return LMSTasksResponse(
            is_connected=True,
            student_name=student_name,
            total_pending=len(pending),
            total_submitted=len(submitted),
            urgent_count=len(urgent),
            tasks=items,
            error_message=None,
        )
    except Exception as exc:
        logger.error("Error retrieving LMS tasks for user %s: %s", user.id, exc)
        return LMSTasksResponse(
            is_connected=True,
            student_name=None,
            total_pending=0,
            total_submitted=0,
            urgent_count=0,
            tasks=[],
            error_message=f"Failed to load LMS tasks: {str(exc)}",
        )


@app.post("/api/lms/refresh", response_model=LMSTasksResponse, tags=["LMS"])
async def refresh_lms_tasks(user: AuthenticatedUser = Depends(get_current_user)):
    """Force fresh LMS data fetch."""
    return await get_lms_tasks(force_refresh=True, user=user)


@app.get("/api/outpass/history", response_model=OutpassHistoryResponse, tags=["Outpass"])
async def get_outpass_history(user: AuthenticatedUser = Depends(get_current_user)):
    """Retrieve latest outpass request and recent history from ERP."""
    erp_creds = credentials.load_user_credentials(user.id)
    if not erp_creds:
        return OutpassHistoryResponse(
            has_active=False,
            active_outpass=None,
            history=[],
            error_message="ERP account not connected. Please log in via Telegram first.",
        )

    try:
        raw_history = await outpass_api.fetch_outpass_history(user_id=user.id)
        records = [OutpassRecord(**r) for r in raw_history]

        active_rec = None
        for r in records:
            st = r.status.lower()
            if not any(w in st for w in ["cancel", "reject"]):
                active_rec = r
                break

        return OutpassHistoryResponse(
            has_active=bool(active_rec),
            active_outpass=active_rec,
            history=records,
            error_message=None,
        )
    except Exception as exc:
        logger.error("Error retrieving outpass history for user %s: %s", user.id, exc)
        return OutpassHistoryResponse(
            has_active=False,
            active_outpass=None,
            history=[],
            error_message=f"Failed to fetch outpass records: {str(exc)}",
        )


@app.get("/api/outpass/allowed-times", response_model=AllowedInTimesResponse, tags=["Outpass"])
async def get_allowed_in_times(
    out_time: str = Query(..., description="Selected out time in HH:MM:00 format"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Query ERP AJAX service for allowable in-times given a selected departure time."""
    try:
        allowed = await outpass_api.get_available_in_times(out_time, user_id=user.id)
    except Exception as exc:
        logger.warning(
            "Could not fetch allowed in-times from ERP for user %s: %s. Generating standard slots.",
            user.id,
            exc,
        )
        try:
            h, m, _ = map(int, out_time.split(":"))
            allowed = []
            for hour in range(h, 24):
                for minute in (0, 30):
                    if hour == h and minute <= m:
                        continue
                    allowed.append(f"{hour:02d}:{minute:02d}:00")
        except Exception:
            allowed = ["18:00:00", "18:30:00", "19:00:00", "19:30:00", "20:00:00", "20:30:00", "21:00:00", "21:30:00", "22:00:00"]

    return AllowedInTimesResponse(out_time=out_time, allowed_times=allowed)


@app.post("/api/outpass/submit-local", response_model=OutpassSubmitResponse, tags=["Outpass"])
async def submit_local_outpass(
    req: LocalOutpassRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Submit local outing (hours) outpass request with per-user lock and rate limiting."""
    erp_creds = credentials.load_user_credentials(user.id)
    if not erp_creds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ERP account not linked. Please link your credentials first.",
        )

    try:
        success, msg = await workload_manager.submit_job(
            workload_manager.outpass_queue,
            user.id,
            "submit_local_outpass",
            "outpass",
            outpass_api.submit_local_outpass,
            date=req.date,
            out_time=req.out_time,
            in_time=req.in_time,
            reason=req.reason,
            meals=req.meals,
            user_id=user.id,
        )
        return OutpassSubmitResponse(success=success, message=msg)
    except Exception as exc:
        logger.error("Outpass submission failed for user %s: %s", user.id, exc)
        return OutpassSubmitResponse(success=False, message=f"Submission error: {str(exc)}")


@app.post("/api/outpass/submit-home", response_model=OutpassSubmitResponse, tags=["Outpass"])
async def submit_home_outpass(
    req: HomeOutpassRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Submit home outing (days) outpass request with per-user lock and priority queue."""
    erp_creds = credentials.load_user_credentials(user.id)
    if not erp_creds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ERP account not linked. Please link your credentials first.",
        )

    try:
        success, msg = await workload_manager.submit_job(
            workload_manager.outpass_queue,
            user.id,
            "submit_home_outpass",
            "outpass",
            outpass_api.submit_home_outpass,
            out_datetime=req.out_datetime,
            in_datetime=req.in_datetime,
            reason=req.reason,
            user_id=user.id,
        )
        return OutpassSubmitResponse(success=success, message=msg)
    except Exception as exc:
        logger.error("Home outpass submission failed for user %s: %s", user.id, exc)
        return OutpassSubmitResponse(success=False, message=f"Submission error: {str(exc)}")


class MorningSettingUpdate(BaseModel):
    enabled: bool


@app.post("/api/settings/morning", tags=["Settings"])
async def update_morning_alerts(
    body: MorningSettingUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Toggle automated morning timetable dispatch preference."""
    storage.set_user_morning_enabled(user.id, body.enabled)
    return {"success": True, "morning_alerts_enabled": body.enabled}


@app.get("/api/dashboard", response_model=DashboardResponse, tags=["Dashboard"])
async def get_dashboard_summary(user: AuthenticatedUser = Depends(get_current_user)):
    """
    Consolidated dashboard payload:
    - Student info & system connection status
    - Next / current class card
    - Today's class count
    - Urgent LMS deadline card & count
    - Active outpass status card
    """
    profile = await get_my_profile(user)
    today = _today_day_name()

    next_class = None
    classes_today_count = 0

    # 1. Timetable snapshot
    cached_html = storage.load_morning_cache(user.id)
    if cached_html:
        try:
            days = parse_timetable_structured(cached_html)
            for d in days:
                if d["day"].lower() == today.lower():
                    classes_today_count = d["total_classes"]
                    next_class = _compute_next_class(d["classes"])
                    break
        except Exception as exc:
            logger.warning("Could not compute next class from morning cache: %s", exc)

    # 2. LMS task snapshot
    pending_tasks_count = 0
    urgent_tasks_count = 0
    urgent_preview = None

    if profile.lms.connected:
        try:
            tasks = await LMSService.fetch_all_pending_tasks(user.id, force_refresh=False)
            items = [_map_lms_task(t) for t in tasks]
            pending = [it for it in items if not it.is_submitted]
            pending_tasks_count = len(pending)

            urgents = [it for it in pending if it.urgency == "urgent"]
            urgent_tasks_count = len(urgents)

            if urgents:
                urgent_preview = min(urgents, key=lambda x: x.seconds_remaining)
            elif pending:
                urgent_preview = min(pending, key=lambda x: x.seconds_remaining)
        except Exception as exc:
            logger.warning("Could not fetch LMS tasks for dashboard: %s", exc)

    # 3. Active outpass snapshot
    active_outpass = None
    if profile.erp.connected:
        try:
            history = await outpass_api.fetch_outpass_history(user_id=user.id)
            if history:
                rec = OutpassRecord(**history[0])
                st = rec.status.lower()
                if not any(w in st for w in ["cancel", "reject"]):
                    active_outpass = rec
        except Exception as exc:
            logger.warning("Could not fetch outpass history for dashboard: %s", exc)

    return DashboardResponse(
        user=profile,
        next_class=next_class,
        classes_today_count=classes_today_count,
        pending_tasks_count=pending_tasks_count,
        urgent_tasks_count=urgent_tasks_count,
        urgent_task_preview=urgent_preview,
        active_outpass=active_outpass,
        today_day_name=today,
    )


# ---------------------------------------------------------------------------
# Static Mini App Frontend Mount
# ---------------------------------------------------------------------------

frontend_dir = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "dist")
)
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    logger.info("Mounted static frontend from %s", frontend_dir)
