import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_TELEGRAM_USER_ID = int(os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "0"))

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

ERP_USERNAME = os.getenv("ERP_USERNAME", "")
ERP_PASSWORD = os.getenv("ERP_PASSWORD", "")

# CAPTCHA solving (optional). Set CAPTCHA_SERVICE to "2captcha" and provide your API key
# to enable automatic CAPTCHA solving. Leave blank to solve CAPTCHA manually in the browser.
CAPTCHA_SERVICE = os.getenv("CAPTCHA_SERVICE", "")  # e.g. "2captcha"
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")
CAPTCHA_SOLVE_TIMEOUT_SECONDS = int(os.getenv("CAPTCHA_SOLVE_TIMEOUT_SECONDS", "120"))

SESSION_FILE = "session.json"
