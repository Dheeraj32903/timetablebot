import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MongoDB Driver / Client Setup
# ---------------------------------------------------------------------------
_mongo_client = None
_mongo_db = None
_mongo_available = False


def _init_mongodb() -> bool:
    """Initialize MongoDB client if MONGODB_URI is provided."""
    global _mongo_client, _mongo_db, _mongo_available
    if not config.MONGODB_URI:
        return False

    try:
        from pymongo import MongoClient
        try:
            import certifi
            ca_file = certifi.where()
        except ImportError:
            ca_file = None

        mongo_kwargs = {
            "serverSelectionTimeoutMS": 5000,
            "connectTimeoutMS": 5000,
            "socketTimeoutMS": 5000,
        }
        if ca_file:
            mongo_kwargs["tlsCAFile"] = ca_file

        _mongo_client = MongoClient(
            config.MONGODB_URI,
            **mongo_kwargs,
        )
        # Test connection
        _mongo_client.admin.command("ping")
        _mongo_db = _mongo_client[config.MONGODB_DB_NAME]
        _mongo_available = True
        logger.info("Successfully connected to MongoDB database '%s'", config.MONGODB_DB_NAME)
        return True
    except Exception as exc:
        logger.warning("MongoDB connection failed or unavailable: %s. Falling back to local storage.", exc)
        _mongo_available = False
        return False


def is_mongodb_active() -> bool:
    global _mongo_available
    if _mongo_client is None:
        _init_mongodb()
    return _mongo_available


# ---------------------------------------------------------------------------
# Local SQLite Fallback Database Setup
# ---------------------------------------------------------------------------
_SQLITE_PATH = config.DATA_DIR / "timetablebot.db"


def _get_sqlite_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_SQLITE_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            password TEXT,
            session_cookies TEXT,
            csrf_token TEXT,
            morning_enabled INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Credentials Storage Operations
# ---------------------------------------------------------------------------

def save_user_credentials(user_id: int, username_encrypted: str, password_encrypted: str) -> None:
    """Save encrypted credentials for a user in MongoDB or local SQLite."""
    now_iso = datetime.now(timezone.utc).isoformat()

    if is_mongodb_active():
        try:
            _mongo_db.users.update_one(
                {"_id": int(user_id)},
                {
                    "$set": {
                        "username": username_encrypted,
                        "password": password_encrypted,
                        "encrypted": True,
                        "updated_at": now_iso,
                    },
                    "$setOnInsert": {
                        "created_at": now_iso,
                        "morning_enabled": False,
                    },
                },
                upsert=True,
            )
            _sync_local_backup()
            return
        except Exception as exc:
            logger.error("Error saving credentials to MongoDB: %s. Writing to SQLite fallback.", exc)

    # SQLite Fallback
    try:
        with _get_sqlite_conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, password, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    password = excluded.password,
                    updated_at = excluded.updated_at;
                """,
                (int(user_id), username_encrypted, password_encrypted, now_iso, now_iso),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Error saving credentials to SQLite: %s", exc)

    _sync_local_backup()


def load_user_credentials(user_id: int) -> Optional[dict]:
    """Load encrypted credentials record for a specific user."""
    if is_mongodb_active():
        try:
            doc = _mongo_db.users.find_one({"_id": int(user_id)})
            if doc:
                if doc.get("username") and doc.get("password"):
                    return {
                        "username": doc["username"],
                        "password": doc["password"],
                    }
                return None
        except Exception as exc:
            logger.warning("Error loading credentials from MongoDB: %s. Trying SQLite.", exc)

    # SQLite Fallback
    try:
        with _get_sqlite_conn() as conn:
            cur = conn.execute(
                "SELECT username, password FROM users WHERE user_id = ?", (int(user_id),)
            )
            row = cur.fetchone()
            if row:
                if row[0] and row[1]:
                    return {"username": row[0], "password": row[1]}
                return None
    except Exception as exc:
        logger.error("Error reading credentials from SQLite: %s", exc)

    # Legacy credentials.json fallback
    if config.CREDENTIALS_FILE.exists():
        try:
            data = json.loads(config.CREDENTIALS_FILE.read_text(encoding="utf-8"))
            record = data.get(str(user_id))
            if record and record.get("username") and record.get("password"):
                return {
                    "username": record["username"],
                    "password": record["password"],
                }
        except Exception:
            pass

    return None


def delete_user_credentials(user_id: int) -> None:
    """Delete a user's stored credentials and session."""
    if is_mongodb_active():
        try:
            _mongo_db.users.update_one(
                {"_id": int(user_id)},
                {"$unset": {"username": "", "password": "", "session": ""}},
            )
        except Exception as exc:
            logger.error("Error deleting credentials in MongoDB: %s", exc)

    try:
        with _get_sqlite_conn() as conn:
            conn.execute(
                "UPDATE users SET username = NULL, password = NULL, session_cookies = NULL, csrf_token = NULL WHERE user_id = ?",
                (int(user_id),),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Error deleting credentials in SQLite: %s", exc)

    _sync_local_backup()


def get_all_users() -> dict[str, dict]:
    """Return all stored users with their encrypted credentials."""
    users_dict = {}

    if is_mongodb_active():
        try:
            cursor = _mongo_db.users.find({"username": {"$exists": True, "$ne": ""}})
            for doc in cursor:
                uid = str(doc["_id"])
                users_dict[uid] = {
                    "username": doc.get("username", ""),
                    "password": doc.get("password", ""),
                }
            if users_dict:
                return users_dict
        except Exception as exc:
            logger.warning("Error fetching all users from MongoDB: %s", exc)

    # SQLite Fallback
    try:
        with _get_sqlite_conn() as conn:
            cur = conn.execute("SELECT user_id, username, password FROM users WHERE username IS NOT NULL AND password IS NOT NULL")
            for row in cur.fetchall():
                users_dict[str(row[0])] = {"username": row[1], "password": row[2]}
    except Exception as exc:
        logger.error("Error fetching all users from SQLite: %s", exc)

    # Legacy JSON file fallback if empty
    if not users_dict and config.CREDENTIALS_FILE.exists():
        try:
            data = json.loads(config.CREDENTIALS_FILE.read_text(encoding="utf-8"))
            for uid, rec in data.items():
                if rec.get("username") and rec.get("password"):
                    users_dict[str(uid)] = {
                        "username": rec["username"],
                        "password": rec["password"],
                    }
        except Exception:
            pass

    return users_dict


# ---------------------------------------------------------------------------
# Session Storage Operations (Per-User)
# ---------------------------------------------------------------------------

def save_user_session(user_id: int, cookies: dict, csrf_token: Optional[str] = None) -> None:
    """Save active ERP cookies and CSRF token for a specific Telegram user."""
    session_data = {
        "cookies": cookies,
        "csrf_token": csrf_token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if is_mongodb_active():
        try:
            _mongo_db.users.update_one(
                {"_id": int(user_id)},
                {"$set": {"session": session_data}},
                upsert=True,
            )
            return
        except Exception as exc:
            logger.warning("Error saving session to MongoDB: %s", exc)

    # SQLite Fallback
    try:
        with _get_sqlite_conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, session_cookies, csrf_token, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_cookies = excluded.session_cookies,
                    csrf_token = excluded.csrf_token,
                    updated_at = excluded.updated_at;
                """,
                (
                    int(user_id),
                    json.dumps(cookies),
                    csrf_token or "",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Error saving session to SQLite: %s", exc)


def load_user_session(user_id: int) -> Optional[dict]:
    """Load active ERP session (cookies and csrf_token) for a user."""
    if is_mongodb_active():
        try:
            doc = _mongo_db.users.find_one({"_id": int(user_id)})
            if doc and doc.get("session"):
                return doc["session"]
        except Exception as exc:
            logger.warning("Error loading session from MongoDB: %s", exc)

    # SQLite Fallback
    try:
        with _get_sqlite_conn() as conn:
            cur = conn.execute(
                "SELECT session_cookies, csrf_token FROM users WHERE user_id = ?", (int(user_id),)
            )
            row = cur.fetchone()
            if row and row[0]:
                return {
                    "cookies": json.loads(row[0]),
                    "csrf_token": row[1] or "",
                }
    except Exception as exc:
        logger.error("Error loading session from SQLite: %s", exc)

    return None


def delete_user_session(user_id: int) -> None:
    """Clear session cookies for a user."""
    if is_mongodb_active():
        try:
            _mongo_db.users.update_one(
                {"_id": int(user_id)},
                {"$unset": {"session": ""}},
            )
        except Exception as exc:
            logger.warning("Error clearing session in MongoDB: %s", exc)

    try:
        with _get_sqlite_conn() as conn:
            conn.execute(
                "UPDATE users SET session_cookies = NULL, csrf_token = NULL WHERE user_id = ?",
                (int(user_id),),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Error clearing session in SQLite: %s", exc)


# ---------------------------------------------------------------------------
# Morning Alert Preferences (Per-User)
# ---------------------------------------------------------------------------

def is_user_morning_enabled(user_id: int) -> bool:
    """Check if morning alert is enabled for a specific user."""
    if is_mongodb_active():
        try:
            doc = _mongo_db.users.find_one({"_id": int(user_id)})
            if doc and "morning_enabled" in doc:
                return bool(doc["morning_enabled"])
        except Exception as exc:
            logger.warning("Error reading morning setting from MongoDB: %s", exc)

    # SQLite Fallback
    try:
        with _get_sqlite_conn() as conn:
            cur = conn.execute("SELECT morning_enabled FROM users WHERE user_id = ?", (int(user_id),))
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                return bool(row[0])
    except Exception as exc:
        logger.error("Error reading morning setting from SQLite: %s", exc)

    # Fallback to global file if single user
    if config.MORNING_ENABLED_FILE.exists():
        try:
            return config.MORNING_ENABLED_FILE.read_text(encoding="utf-8").strip() == "1"
        except Exception:
            pass

    return False


def set_user_morning_enabled(user_id: int, enabled: bool) -> None:
    """Toggle morning alert for a specific user."""
    if is_mongodb_active():
        try:
            _mongo_db.users.update_one(
                {"_id": int(user_id)},
                {"$set": {"morning_enabled": bool(enabled)}},
                upsert=True,
            )
            return
        except Exception as exc:
            logger.error("Error setting morning preference in MongoDB: %s", exc)

    try:
        with _get_sqlite_conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, morning_enabled)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    morning_enabled = excluded.morning_enabled;
                """,
                (int(user_id), 1 if enabled else 0),
            )
            conn.commit()
    except Exception as exc:
        logger.error("Error setting morning preference in SQLite: %s", exc)

    try:
        config.MORNING_ENABLED_FILE.write_text("1" if enabled else "0", encoding="utf-8")
    except Exception:
        pass


def get_all_morning_users() -> list[int]:
    """Get all user IDs that have morning alert enabled."""
    user_ids = []

    if is_mongodb_active():
        try:
            cursor = _mongo_db.users.find({"morning_enabled": True})
            for doc in cursor:
                user_ids.append(int(doc["_id"]))
            return user_ids
        except Exception as exc:
            logger.warning("Error fetching morning users from MongoDB: %s", exc)

    try:
        with _get_sqlite_conn() as conn:
            cur = conn.execute("SELECT user_id FROM users WHERE morning_enabled = 1")
            for row in cur.fetchall():
                user_ids.append(int(row[0]))
    except Exception as exc:
        logger.error("Error fetching morning users from SQLite: %s", exc)

    return user_ids


# ---------------------------------------------------------------------------
# Migration & Backup Sync
# ---------------------------------------------------------------------------

def _sync_local_backup() -> None:
    """Write current credentials to credentials.json as a local backup."""
    try:
        all_users = get_all_users()
        payload = {}
        for uid, rec in all_users.items():
            payload[uid] = {
                "username": rec["username"],
                "password": rec["password"],
                "encrypted": True,
            }
        config.CREDENTIALS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Local backup sync skipped: %s", exc)


def auto_migrate_local_data() -> None:
    """
    On startup, import any existing credentials from credentials.json,
    SQLite database, session.json, and .morning_enabled into MongoDB.
    """
    # 1. If MongoDB is active, migrate any records from SQLite to MongoDB
    if is_mongodb_active():
        try:
            with _get_sqlite_conn() as conn:
                cur = conn.execute("SELECT user_id, username, password, morning_enabled FROM users WHERE username IS NOT NULL AND password IS NOT NULL")
                for row in cur.fetchall():
                    uid = int(row[0])
                    doc = _mongo_db.users.find_one({"_id": uid})
                    if not doc or not doc.get("username"):
                        save_user_credentials(uid, row[1], row[2])
                        if row[3]:
                            set_user_morning_enabled(uid, bool(row[3]))
                        logger.info("Migrated user %d from SQLite to MongoDB Atlas.", uid)
        except Exception as exc:
            logger.debug("SQLite to MongoDB migration check: %s", exc)

    # 2. Migrate credentials.json
    for cred_file in [config.PROJECT_ROOT / "credentials.json", config.CREDENTIALS_FILE]:
        if cred_file.exists():
            try:
                data = json.loads(cred_file.read_text(encoding="utf-8"))
                for user_id_str, rec in data.items():
                    if rec.get("username") and rec.get("password"):
                        uid = int(user_id_str)
                        if is_mongodb_active():
                            doc = _mongo_db.users.find_one({"_id": uid})
                            if not doc or not doc.get("username"):
                                save_user_credentials(uid, rec["username"], rec["password"])
                                logger.info("Migrated user %d from %s into MongoDB Atlas.", uid, cred_file.name)
                        else:
                            existing = load_user_credentials(uid)
                            if not existing:
                                save_user_credentials(uid, rec["username"], rec["password"])
            except Exception as exc:
                logger.warning("Auto-migration of %s failed: %s", cred_file, exc)

    # 3. Migrate global morning setting if set
    if config.MORNING_ENABLED_FILE.exists():
        try:
            if config.MORNING_ENABLED_FILE.read_text(encoding="utf-8").strip() == "1":
                for uid_str in get_all_users().keys():
                    set_user_morning_enabled(int(uid_str), True)
        except Exception:
            pass


# Initialize and migrate on import
_init_mongodb()
auto_migrate_local_data()
