# Project paths
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
MODEL_DIR = PROJECT_ROOT / "model"
SESSION_FILE = PROJECT_ROOT / "session.json"

# Telegram
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_TELEGRAM_USER_ID = int(os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "0"))

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
