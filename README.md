# KL University ERP Timetable Telegram Bot

A private Telegram bot that fetches your personal timetable from the KL University ERP (`newerp.kluniversity.in`).

It logs in automatically using your ERP credentials, solves the CAPTCHA locally with an ONNX model, and sends your current timetable to your Telegram chat.

## How it works

1. You send `/login` from Telegram.
2. The bot asks for your ERP username and password in chat (or uses the ones in `.env`).
3. The bot logs in to the ERP in the background, solves the CAPTCHA locally, and saves the session.
4. You send `/timetable`.
5. The bot calls the ERP timetable endpoint, parses the HTML table, and sends it to your Telegram chat.
6. Once Telegram has delivered the message, you can read it offline.

## Security

- The bot only responds to **your** Telegram user ID.
- Credentials given in Telegram chat are kept in memory only during login and are **not saved to disk**.
- ERP session cookies are stored locally in `session.json`.
- CAPTCHA solving happens locally on your machine using the ONNX model in `model/`.

## Requirements

- Python 3.10+
- Windows / macOS / Linux
- ONNX model files: `model/crnn.onnx` and `model/crnn.json`

## Project structure

```
.
├── bot.py              # Telegram bot entry point
├── config.py           # Configuration and project paths
├── requirements.txt
├── .env
├── erp/
│   ├── client.py       # HTTP client for ERP login + timetable
│   ├── session.py      # Session cookie persistence
│   └── captcha_solver.py  # Local ONNX CAPTCHA solver
├── parser/
│   └── timetable.py    # ERP timetable HTML parser
└── model/
    ├── crnn.onnx
    └── crnn.json
```

## Setup

1. Clone or copy this project to your machine.
2. Make sure the `model/` folder contains `crnn.onnx` and `crnn.json`.
3. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

4. Copy the environment template and fill it in:

```bash
copy .env.example .env       # Windows
# cp .env.example .env       # macOS/Linux
```

5. Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_BotFather
AUTHORIZED_TELEGRAM_USER_ID=your_telegram_user_id
```

- Get your bot token from [@BotFather](https://t.me/BotFather).
- Get your Telegram user ID from [@userinfobot](https://t.me/userinfobot).
- `ERP_TIMETABLE_URL` already contains the real KL University timetable route. If your academic year or semester changes, update the `academicyear` and `semesterid` values in that URL.
- Optional: set `ERP_USERNAME` and `ERP_PASSWORD` in `.env` to skip typing them in Telegram chat.

6. Run the bot:

```bash
python bot.py
```

7. In Telegram, send `/login` to your bot, then follow the prompts. If credentials are in `.env`, `/login` will log in immediately.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show help. |
| `/login` | Log in to the ERP automatically (asks for credentials in chat if not in `.env`). |
| `/timetable` | Fetch and send the latest timetable. |
| `/status` | Check whether an ERP session is saved. |
| `/logout` | Clear the saved session. |

## Troubleshooting

- **"No active ERP session"** — run `/login` again.
- **"ERP session expired"** — the ERP logged you out. Run `/login` again.
- **"CAPTCHA solver returned empty text"** — make sure `model/crnn.onnx` and `model/crnn.json` exist.
- **"ERP login failed"** — check your username and password.
- **Timetable table looks wrong** — the HTML parser uses the largest table in the page. If the ERP layout changes, update `parser/timetable.py`.

## Notes

- This bot is designed for a single authorized user only.
- The ERP is always the source of truth: every `/timetable` request fetches the current timetable from the ERP in real time.
- No browser window is needed; everything is done through HTTP requests.
