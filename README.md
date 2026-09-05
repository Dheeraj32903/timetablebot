# KL University ERP Timetable & LMS Submissions Bot 🎓

A fast, secure, and private Telegram bot that unifies your personal timetable from KL University ERP (`newerp.kluniversity.in`) and pending assignment deadlines from KL Moodle LMS (`lms.kluniversity.in`).

Features local automated CAPTCHA solving with ONNX, 256-bit Fernet encrypted multi-user credentials, automated background deadline alerts (48h & 6h reminders), instant new task announcements, and 7:00 AM daily morning timetable delivery.

---

## ✨ Features

### 📅 ERP Timetable
- 🔘 **Interactive Telegram UI & Menus**: Full support for persistent reply keyboards, interactive inline buttons, day selector grids, in-place day navigation (`[◀️ Prev]`, `[Next ▶️]`, `[🔄 Refresh]`), and native Telegram `/` popup command menus.
- 🧩 **Local AI CAPTCHA Solver**: Solves ERP login verification codes locally using an ONNX CRNN model with 0 external API dependencies.
- 📱 **Aesthetic Card Design**: Clean, card-based layout with slot durations, 12-hour AM/PM timings, room numbers, section pills, and course title mapping.
- 📚 **Full Course Catalog**: Automatically maps ERP codes to human-readable titles (e.g. `24CC3014-S` ➔ *AWS Certified Solutions Architect*, `24AD3107-L` ➔ *Data Science*).
- 📅 **Day & Week Filtering**: Supports `/timetable today`, `/timetable tomorrow`, `/timetable wed`, and full-week view `/week` with one-tap inline buttons.
- 🌅 **Daily Morning Dispatch**: Automatically delivers your daily schedule every morning at 7:00 AM IST (`/morning`).
- 👥 **Friend Timetable**: Check a classmate's schedule on-demand without overriding your saved credentials (`/friend`).

### 📝 LMS Submissions & Deadlines
- ⚡ **Ultra-Fast Submissions Tracker (`/submissions`)**: Fetches all pending Home Assignments (HA), Active Learning Modules (ALM), and Quizzes across your enrolled courses in ~2 seconds (with memory caching for instant loads).
- 📢 **Automated Background Reminders**: Background jobs monitor deadlines and notify you 2 days (48h) and 6 hours before deadlines so you never miss a submission.
- 🆕 **New Task Alerts**: Automatically alerts you when a professor uploads a new assignment or quiz.
- 🔗 **1-Tap Direct Links**: Inline buttons take you directly to the submission portal.

### 🔒 Enterprise-Grade Security & Privacy
- 🔐 **Authenticated Fernet Encryption**: All ERP and LMS student credentials, passwords, and Moodle API tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before storing in MongoDB Atlas.
- 🚫 **Zero Plaintext Storage**: Plaintext passwords are never saved to disk or database.
- 🗑️ **Auto-Deleting Chat Secrets**: Password messages sent during login are instantly deleted from Telegram chat.
- 🚪 **Complete User Control**: Run `/logout` or `/lms_logout` to wipe credentials. Blocking or stopping the bot automatically triggers a full purge of your data from MongoDB.

---

## 📁 Project Structure

```text
.
├── bot.py                # Telegram bot entry point, command handlers & background jobs
├── config.py             # Configuration & environment variable loader
├── credentials.py        # 256-bit Fernet encryption for ERP & LMS credentials and tokens
├── storage.py            # MongoDB Atlas database operations & user state management
├── requirements.txt      # Python package dependencies
├── Dockerfile            # Production Docker image definition
├── docker-compose.yml    # Multi-container orchestration
├── .dockerignore         # Docker context exclusions
├── render.yaml           # Render Cloud blueprint specification
├── .env.example          # Environment variables template
├── erp/
│   ├── client.py         # Asynchronous HTTP client for ERP login & timetable fetch
│   ├── session.py        # ERP session & cookie management
│   └── captcha_solver.py # Local ONNX CRNN CAPTCHA solver
├── lms/
│   ├── __init__.py       # Package exports
│   ├── client.py         # High-speed Moodle Mobile REST API client with connection pooling
│   └── service.py        # Concurrent task aggregator, status verification & Telegram card formatter
├── parser/
│   └── timetable.py      # Timetable HTML parser, course catalog & card formatter
└── model/
    ├── crnn.onnx         # Pre-trained CAPTCHA recognition model
    └── crnn.json         # Character dictionary
```

---

## 🚀 Setup & Local Running

### 1. Prerequisites
- Python 3.10 or higher
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- Your Telegram User ID from [@userinfobot](https://t.me/userinfobot)
- Free MongoDB Atlas database cluster (or local MongoDB)

### 2. Installation

```bash
# 1. Clone repository
git clone https://github.com/Dheeraj32903/timetablebot.git
cd timetablebot

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate    # Linux / macOS
# .venv\Scripts\activate     # Windows

# 3. Install dependencies
pip install -r requirements.txt
```

### 3. Configuration

Copy the example configuration file:
```bash
cp .env.example .env
```

Edit `.env` with your values:
```env
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
AUTHORIZED_TELEGRAM_USER_IDS=6986045748,7337604045

# MongoDB Atlas Cloud Database
MONGODB_URI=mongodb+srv://<username>:<password>@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB_NAME=klu_timetable_bot

# Optional: Set a specific 32-byte Fernet key (if blank, a stable key is auto-derived from your bot token)
CREDENTIALS_ENCRYPTION_KEY=

# Morning broadcast time (IST 24-hour format)
MORNING_TIMETABLE_HOUR=7
MORNING_TIMETABLE_MINUTE=0
```

### 4. Run the Bot

```bash
python bot.py
```

---

## 🤖 Bot Commands

| Command | Description |
| :--- | :--- |
| `/start` | Welcome message & command list |
| `/timetable` | View today's classes, timings & rooms |
| `/timetable <day>` | View timetable for a specific day (`today`, `tomorrow`, `mon`, etc.) |
| `/week` | Full weekly timetable view |
| `/submissions` | View pending assignments, ALMs, quizzes & LMS deadlines |
| `/friend` | Fetch another student's timetable without overriding your saved account |
| `/login` | Connect & save ERP credentials (encrypted) |
| `/lms_login` | Connect & save KL LMS credentials (encrypted) |
| `/lms_logout` | Disconnect LMS credentials and clear alert history |
| `/morning` | Toggle automated daily 7:00 AM IST morning timetable delivery |
| `/status` | Check current active login session status |
| `/description` | View project description and key benefits |
| `/privacy` | View data storage, encryption & privacy policy |
| `/logout` | Log out ERP and clear saved credentials |

---

## 🌐 Production Deployment

### Option A: Render Cloud Deployment (Recommended)

This repository includes a [`render.yaml`](file:///c:/Desktop/my%20workspace/telegrambot/render.yaml) blueprint for 1-click deployment on Render:

1. Push your repository to GitHub / GitLab.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** ➔ **Blueprint**.
3. Connect your repository. Render will automatically detect `render.yaml`.
4. Fill in the secret environment variables:
   - `TELEGRAM_BOT_TOKEN`
   - `AUTHORIZED_TELEGRAM_USER_IDS`
   - `MONGODB_URI`
   - `CREDENTIALS_ENCRYPTION_KEY` (optional)
5. Click **Apply**. Render will build the service and start the bot with an automated HTTP health check responder on `PORT`.

> **Note**: You can also deploy as a Render **Background Worker** (`type: worker`), which does not require an open port or health check.

---

### Option B: Running with Docker & Docker Compose

1. Clone repository and set up `.env`:
   ```bash
   cp .env.example .env
   nano .env
   ```
2. Start the container in background mode:
   ```bash
   docker compose up -d --build
   ```
3. Check live logs:
   ```bash
   docker compose logs -f
   ```
4. Stop or restart:
   ```bash
   docker compose down
   ```

---

### Option C: Running with `systemd` on Linux VPS (Ubuntu / Debian)

1. Create a service file:
   ```bash
   sudo nano /etc/systemd/system/timetablebot.service
   ```

2. Add the configuration (adjust paths and user):
   ```ini
   [Unit]
   Description=KL University Timetable & LMS Bot
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/timetablebot
   ExecStart=/home/ubuntu/timetablebot/.venv/bin/python bot.py
   Restart=always
   RestartSec=10
   EnvironmentFile=/home/ubuntu/timetablebot/.env

   [Install]
   WantedBy=multi-user.target
   ```

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable timetablebot
   sudo systemctl start timetablebot
   ```

4. Check logs:
   ```bash
   sudo journalctl -u timetablebot -f
   ```

---

## 🔒 Security & Privacy

- **Fernet 256-bit Encryption**: All credentials and tokens stored in MongoDB are encrypted using authenticated Fernet (AES-128-CBC + HMAC-SHA256).
- **Auto Chat Wipes**: Plaintext passwords sent in Telegram chat are deleted immediately.
- **Whitelist Authorization**: Optional restriction via `AUTHORIZED_TELEGRAM_USER_IDS` to restrict bot usage to specific student Telegram IDs.
- **Ephemeral Friend Mode**: `/friend` credentials are processed in-memory for a single fetch and never written to the database.
- **Automatic Account Purge**: Stopping, blocking, or deleting chat with the bot permanently deletes all associated account records and tokens from MongoDB.
