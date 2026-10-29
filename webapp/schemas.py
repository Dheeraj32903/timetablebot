from typing import Dict, List, Optional
from pydantic import BaseModel, Field


# --- Profile & System Status ---

class IntegrationStatus(BaseModel):
    connected: bool
    username: Optional[str] = None
    status_text: str
    details: Optional[str] = None


class UserProfileResponse(BaseModel):
    id: int
    first_name: str
    last_name: str = ""
    telegram_username: str = ""
    student_id: Optional[str] = None
    erp: IntegrationStatus
    lms: IntegrationStatus
    morning_alerts_enabled: bool
    morning_alert_time: str = "06:45 AM IST"


# --- Timetable Schemas ---

class ClassSlot(BaseModel):
    slot_label: str
    start_time: str
    end_time: str
    subject_code: str
    subject_title: Optional[str] = None
    class_type: str
    room: str
    section: str


class DaySchedule(BaseModel):
    day: str
    full_day_name: str
    total_classes: int
    is_today: bool = False
    classes: List[ClassSlot] = []


class TimetableResponse(BaseModel):
    current_day: str
    has_timetable: bool
    is_live_synced: bool
    days: List[DaySchedule] = []
    error_message: Optional[str] = None


# --- LMS / Submissions Schemas ---

class LMSTaskItem(BaseModel):
    id: str
    title: str
    course_name: str
    task_type: str
    due_timestamp: int
    due_date_str: str
    time_left_str: str
    seconds_remaining: float
    is_submitted: bool
    urgency: str  # 'urgent' (<24h), 'due_soon' (<48h), 'upcoming', 'submitted'
    url: str = ""


class LMSTasksResponse(BaseModel):
    is_connected: bool
    student_name: Optional[str] = None
    total_pending: int = 0
    total_submitted: int = 0
    urgent_count: int = 0
    tasks: List[LMSTaskItem] = []
    error_message: Optional[str] = None


# --- Hostel Outpass Schemas ---

class OutpassRecord(BaseModel):
    id: str
    student_id: str
    outing_date: str
    out_time: str
    in_time: str
    reason: str
    warden_name: Optional[str] = None
    status: str
    approved_by: Optional[str] = None
    cancellation_status: Optional[str] = None
    view_url: Optional[str] = None


class OutpassHistoryResponse(BaseModel):
    has_active: bool
    active_outpass: Optional[OutpassRecord] = None
    history: List[OutpassRecord] = []
    error_message: Optional[str] = None


class AllowedInTimesResponse(BaseModel):
    out_time: str
    allowed_times: List[str] = []


class LocalOutpassRequest(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    out_time: str = Field(..., description="HH:MM:SS")
    in_time: str = Field(..., description="HH:MM:SS")
    reason: str = Field(..., min_length=2, max_length=150)
    meals: Dict[str, str] = Field(default_factory=lambda: {"breakfast": "NO", "lunch": "NO", "snack": "NO", "dinner": "NO"})


class HomeOutpassRequest(BaseModel):
    out_datetime: str = Field(..., description="YYYY-MM-DD HH:MM:SS")
    in_datetime: str = Field(..., description="YYYY-MM-DD HH:MM:SS")
    reason: str = Field(..., min_length=2, max_length=150)


class OutpassSubmitResponse(BaseModel):
    success: bool
    message: str
    outpass_id: Optional[str] = None


# --- Home Dashboard Aggregated Schema ---

class NextClassInfo(BaseModel):
    subject_title: str
    subject_code: str
    class_type: str
    room: str
    section: str
    start_time: str
    end_time: str
    status: str  # 'in_progress', 'upcoming', 'completed'
    time_badge: str


class DashboardResponse(BaseModel):
    user: UserProfileResponse
    next_class: Optional[NextClassInfo] = None
    classes_today_count: int = 0
    pending_tasks_count: int = 0
    urgent_tasks_count: int = 0
    urgent_task_preview: Optional[LMSTaskItem] = None
    active_outpass: Optional[OutpassRecord] = None
    today_day_name: str

