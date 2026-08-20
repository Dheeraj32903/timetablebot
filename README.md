# KL University ERP Timetable Telegram Bot 🎓

A fast, secure, and private Telegram bot that fetches and formats your personal timetable directly from the KL University ERP (`newerp.kluniversity.in`).

Features local automated CAPTCHA solving with ONNX, 256-bit Fernet encrypted multi-user credentials, smart class slot mapping, aesthetic card views with emojis, and daily morning timetable broadcasts.

---

## ✨ Features

- 🔘 **Interactive Telegram UI & Menus**: Full support for persistent reply keyboards, interactive inline buttons, day selector grids, in-place day navigation (`[◀️ Prev]`, `[Next ▶️]`, `[🔄 Refresh]`), and native Telegram `/` popup command menus.
- 🔐 **Multi-User Authorization & Security**: Whitelist specific Telegram user IDs. Passwords and usernames are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256).
- 🧩 **Local AI CAPTCHA Solver**: Solves ERP login verification codes locally using an ONNX CRNN model with 0 external API dependencies.
- 📱 **Aesthetic Card Design**: Clean, card-based mobile layout with slot durations, 12-hour AM/PM timings, room numbers, section pills, and period-type emojis:
  - 👨‍🏫 **Lecture**
  - 💻 **Practical Lab & Skill Lab**
  - 📝 **Tutorial**
  - 🛠️ **Project**
- 📚 **Full Course Catalog**: Automatically maps ERP codes to human-readable titles (e.g. `24CC3014-S` ➔ *AWS Certified Solutions Architect (SAA-C03)*, `24AD3107-L` ➔ *Data Science*).
- 📅 **Day & Week Filtering**: Supports `/timetable today`, `/timetable tomorrow`, `/timetable wed`, and full-week view `/week` with one-tap inline buttons.
- 🌅 **Daily Morning Broadcast**: Automatically delivers your daily schedule every morning at 7:00 AM IST (configurable).
- 👥 **Friend Timetable**: Check a friend's/classmate's timetable on-demand without overriding your saved credentials (`/friend`).

---

## 📁 Project Structure

```text
.
├── bot.py                # Telegram bot entry point & command handlers
├── config.py             # Configuration & environment variable loader
├── credentials.py        # 256-bit Fernet encryption & secure credentials storage
├── requirements.txt      # Python package dependencies
├── .env.example          # Environment variables template
├── erp/
│   ├── client.py         # Asynchronous HTTP client for ERP login & timetable fetch
│   ├── session.py        # ERP session & cookie management
│   └── captcha_solver.py # Local ONNX CRNN CAPTCHA solver
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
cp .env.example .env    # Linux / macOS
# copy .env.example .env # Windows
```

Edit `.env` with your values:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
AUTHORIZED_TELEGRAM_USER_IDS=6986045748,7337604045

# MongoDB Atlas Cloud Database (ensures user logins persist across all redeployments)
# Get your free connection string from https://cloud.mongodb.com
MONGODB_URI=mongodb+srv://<username>:<password>@timetablebot.abcde.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB_NAME=klu_timetable_bot

# Optional: Set a specific 32-byte Fernet key (if left blank, a stable key is auto-derived)
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
| `/login` | Interactive ERP login (saves encrypted credentials for auto-login) |
| `/timetable` | View today's timetable |
| `/timetable <day>` | View timetable for a specific day (`today`, `tomorrow`, `mon`, `tue`, etc.) |
| `/week` | View the full week's schedule |
| `/friend` | Fetch another student's timetable without overriding your saved account |
| `/morning` | Toggle automated daily 7:00 AM IST morning timetable delivery |
| `/status` | Check current active login session status |
| `/logout` | Log out and wipe saved credentials from disk |

---

## 🌐 Production Deployment (Linux / AWS / VPS)

### Option A: Running with Docker (Easiest & Cleanest)

1. Make sure Docker and Docker Compose are installed on your server.
2. Clone the repo and configure your `.env`:
   ```bash
   cp .env.example .env
   nano .env
   ```
3. Start the bot in the background:
   ```bash
   docker compose up -d --build
   ```
4. Check live logs:
   ```bash
   docker compose logs -f
   ```
5. Stop the bot:
   ```bash
   docker compose down
   ```

---

### Option B: Running with `systemd` (Without Docker)

1. Create a service file:
```bash
sudo nano /etc/systemd/system/timetablebot.service
```

2. Paste the following configuration (adjust paths and user):
```ini
[Unit]
Description=KL University Timetable Telegram Bot
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

3. Enable and start the service:
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

- **Encrypted at Rest**: User ERP passwords and student registration IDs in `credentials.json` are encrypted using authenticated 256-bit Fernet AES-128-CBC + HMAC-SHA256.
- **Whitelist Protected**: Only authorized Telegram user IDs listed in `AUTHORIZED_TELEGRAM_USER_IDS` can interact with the bot.
- **Local CAPTCHA Processing**: CAPTCHA images are decoded strictly in-memory using the local ONNX runtime model. No images or tokens are sent to third-party APIs.
