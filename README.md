# KL University ERP Timetable Telegram Bot

A private Telegram bot that fetches your personal timetable from the KL University ERP (`newerp.kluniversity.in`).

## How it works

1. You send `/login` from Telegram.
2. The bot opens the ERP login page in a real browser window on your computer.
3. If you configured automatic CAPTCHA solving, the bot fills your credentials, solves the CAPTCHA, and logs in. Otherwise, you complete the CAPTCHA and log in manually.
4. The bot captures the authenticated session cookies and CSRF token.
5. You send `/timetable`.
6. The bot calls the ERP timetable endpoint, parses the HTML table, and sends it to your Telegram chat.
7. Once Telegram has delivered the message, you can read it offline.

## Security

- The bot only responds to **your** Telegram user ID.
- ERP credentials are optional. If you leave them blank in `.env`, you type them into the browser yourself.
- Session cookies are stored locally in `session.json`.

## Requirements

- Python 3.10+
- Windows / macOS / Linux
- A machine that can open a browser (needed for manual CAPTCHA login)

## Setup

1. Clone or copy this project to your machine.
2. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
playwright install chromium
```

3. Copy the environment template and fill it in:

```bash
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

4. Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
AUTHORIZED_TELEGRAM_USER_ID=12345678
ERP_USERNAME=your_erp_username
ERP_PASSWORD=your_erp_password

# Optional: automatic CAPTCHA solving (paid service).
CAPTCHA_SERVICE=2captcha
CAPTCHA_API_KEY=your_2captcha_api_key
CAPTCHA_SOLVE_TIMEOUT_SECONDS=120
```

- Get your bot token from [@BotFather](https://t.me/BotFather).
- Get your Telegram user ID from [@userinfobot](https://t.me/userinfobot).
- `ERP_USERNAME` and `ERP_PASSWORD` are required if you want the bot to log in automatically.
- `ERP_TIMETABLE_URL` already contains the real KL University timetable route.
  If your academic year or semester changes, update the `academicyear` and `semesterid` values in that URL.
- To use automatic CAPTCHA solving, sign up at [2captcha.com](https://2captcha.com), add funds, and paste your API key into `CAPTCHA_API_KEY`.
  Leave `CAPTCHA_SERVICE` blank to solve CAPTCHA manually in the browser.

5. Run the bot:

```bash
python bot.py
```

6. In Telegram, send `/login` to your bot. If automatic CAPTCHA solving is enabled, wait for the bot to log in. Otherwise, complete the CAPTCHA in the browser, then send `/timetable`.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help. |
| `/login` | Open ERP login page and capture session. |
| `/timetable` | Fetch and send the latest timetable. |
| `/status` | Check whether a session is saved. |
| `/logout` | Clear the saved session. |

## Troubleshooting

- **"No active ERP session"** — run `/login` again.
- **"ERP session expired"** — the ERP logged you out. Run `/login` again.
- **CAPTCHA not showing** — make sure `playwright install chromium` completed successfully.
- **Timetable table looks wrong** — the HTML parser uses the largest table in the page. If the ERP layout changes, update `timetable_parser.py`.

## Notes

- This bot is designed for a single authorized user only.
- Automatic CAPTCHA solving uses the paid 2captcha service. The CAPTCHA image is sent to their API and solved by human workers/AI.
- The ERP is always the source of truth: every `/timetable` request fetches the current timetable from the ERP in real time.
- Because the login still uses a real browser, the bot must run on a machine with a display (your laptop/desktop).
