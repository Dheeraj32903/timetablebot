import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.resolve()
MODEL_DIR = PROJECT_ROOT / "model"

# Data directory for persistent files if running locally or with attached volume
_env_data_dir = os.getenv("DATA_DIR", "").strip()
if _env_data_dir:
    DATA_DIR = Path(_env_data_dir).resolve()
elif Path("/data").exists() and os.access("/data", os.W_OK):
    DATA_DIR = Path("/data")
else:
    DATA_DIR = PROJECT_ROOT

DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_FILE = DATA_DIR / "session.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
MORNING_ENABLED_FILE = DATA_DIR / ".morning_enabled"
SECRET_KEY_FILE = DATA_DIR / ".secret.key"

# MongoDB Database Configuration
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "klu_timetable_bot").strip()


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Comma-separated list of authorized Telegram user IDs. Leave empty to allow all users.
# Backward-compatible with the old AUTHORIZED_TELEGRAM_USER_ID variable.
_authorized_ids = os.getenv("AUTHORIZED_TELEGRAM_USER_IDS", "").strip()
if not _authorized_ids:
    _authorized_ids = os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "").strip()
if _authorized_ids:
    AUTHORIZED_TELEGRAM_USER_IDS = {int(uid.strip()) for uid in _authorized_ids.split(",") if uid.strip()}
else:
    AUTHORIZED_TELEGRAM_USER_IDS = set()

# ERP
ERP_BASE_URL = os.getenv("ERP_BASE_URL", "https://newerp.kluniversity.in")
ERP_LOGIN_URL = os.getenv(
    "ERP_LOGIN_URL",
    "https://newerp.kluniversity.in/index.php?r=site%2Flogin",
)
ERP_TIMETABLE_URL = os.getenv(
    "ERP_TIMETABLE_URL",
    "https://newerp.kluniversity.in/index.php?r=timetables%2Funiversitymasteracademictimetableview%2Findividualstudenttimetableget&UniversityMasterAcademicTimetableView%5Bacademicyear%5D=19&UniversityMasterAcademicTimetableView%5Bsemesterid%5D=2",
)
ERP_TIMETABLE_REFERER_URL = os.getenv(
    "ERP_TIMETABLE_REFERER_URL",
    "https://newerp.kluniversity.in/index.php?r=timetables%2Funiversitymasteracademictimetableview%2Findexstudentindisearch",
)

ERP_ACADEMIC_YEAR = os.getenv("ERP_ACADEMIC_YEAR", "19").strip()
ERP_SEMESTER_ID = os.getenv("ERP_SEMESTER_ID", "2").strip()

ERP_USER_AGENT = os.getenv(
    "ERP_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36",
)

# Optional fallback credentials. If not set here, the bot will ask for them in Telegram chat.
ERP_USERNAME = os.getenv("ERP_USERNAME", "")
ERP_PASSWORD = os.getenv("ERP_PASSWORD", "")

# Key used to encrypt stored ERP credentials in credentials.json.
# Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIALS_ENCRYPTION_KEY = os.getenv("CREDENTIALS_ENCRYPTION_KEY", "")

# Daily automatic morning timetable message time (IST, 24-hour format).
# Defaults to 06:45 AM IST (Warmup pre-fetch starts at 06:30 AM IST).
MORNING_TIMETABLE_HOUR = int(os.getenv("MORNING_TIMETABLE_HOUR", "6"))
MORNING_TIMETABLE_MINUTE = int(os.getenv("MORNING_TIMETABLE_MINUTE", "45"))

# Maximum number of automated login retry attempts for CAPTCHA solving.
# Paced with backoff and jitter to prevent hammering ERP servers.
LOGIN_MAX_RETRIES = int(os.getenv("LOGIN_MAX_RETRIES", "6"))

# --- Workload & Worker Concurrency ---
OUTPASS_WORKERS = int(os.getenv("OUTPASS_WORKERS", "3"))
TIMETABLE_WORKERS = int(os.getenv("TIMETABLE_WORKERS", "4"))
LMS_WORKERS = int(os.getenv("LMS_WORKERS", "4"))
PREFETCH_WORKERS = int(os.getenv("PREFETCH_WORKERS", "4"))
TELEGRAM_DELIVERY_WORKERS = int(os.getenv("TELEGRAM_DELIVERY_WORKERS", "2"))

# --- Global Backend Gates ---
GLOBAL_ERP_MAX_CONCURRENCY = int(os.getenv("GLOBAL_ERP_MAX_CONCURRENCY", "5"))
GLOBAL_ERP_MAX_RPS = float(os.getenv("GLOBAL_ERP_MAX_RPS", "5.0"))

GLOBAL_LMS_MAX_CONCURRENCY = int(os.getenv("GLOBAL_LMS_MAX_CONCURRENCY", "8"))
GLOBAL_LMS_MAX_RPS = float(os.getenv("GLOBAL_LMS_MAX_RPS", "8.0"))

# --- Bounded Queue Sizes ---
OUTPASS_QUEUE_MAXSIZE = int(os.getenv("OUTPASS_QUEUE_MAXSIZE", "200"))
TIMETABLE_QUEUE_MAXSIZE = int(os.getenv("TIMETABLE_QUEUE_MAXSIZE", "500"))
LMS_QUEUE_MAXSIZE = int(os.getenv("LMS_QUEUE_MAXSIZE", "500"))
PREFETCH_QUEUE_MAXSIZE = int(os.getenv("PREFETCH_QUEUE_MAXSIZE", "2000"))

# --- Telegram Outbox Throttling ---
TELEGRAM_DELIVERY_MAX_RPS = float(os.getenv("TELEGRAM_DELIVERY_MAX_RPS", "20.0"))

# --- Cache & TTL ---
MORNING_CACHE_TTL_SECONDS = int(os.getenv("MORNING_CACHE_TTL_SECONDS", "14400"))  # 4 hours

