# Project paths
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
MODEL_DIR = PROJECT_ROOT / "model"
SESSION_FILE = PROJECT_ROOT / "session.json"
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
MORNING_ENABLED_FILE = PROJECT_ROOT / ".morning_enabled"

# Telegram
import os
from dotenv import load_dotenv

load_dotenv()

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
    "https://newerp.kluniversity.in/index.php?r=timetables%2Funiversitymasteracademictimetableview%2Findividualstudenttimetableget&UniversityMasterAcademicTimetableView%5Bacademicyear%5D=29&UniversityMasterAcademicTimetableView%5Bsemesterid%5D=1",
)
ERP_TIMETABLE_REFERER_URL = os.getenv(
    "ERP_TIMETABLE_REFERER_URL",
    "https://newerp.kluniversity.in/index.php?r=timetables%2Funiversitymasteracademictimetableview%2Findexstudentindisearch",
)

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
MORNING_TIMETABLE_HOUR = int(os.getenv("MORNING_TIMETABLE_HOUR", "7"))
MORNING_TIMETABLE_MINUTE = int(os.getenv("MORNING_TIMETABLE_MINUTE", "0"))
