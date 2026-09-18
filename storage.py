import logging
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MongoDB Database Setup
# ---------------------------------------------------------------------------

if not config.MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI environment variable is not configured. "
        "MongoDB is required for persistent storage on Render."
    )

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

    _mongo_client = MongoClient(config.MONGODB_URI, **mongo_kwargs)
    # Test connection
    _mongo_client.admin.command("ping")
    _mongo_db = _mongo_client[config.MONGODB_DB_NAME]
    logger.info("Successfully connected to MongoDB database '%s'", config.MONGODB_DB_NAME)
except Exception as exc:
    logger.critical("Failed to connect to MongoDB: %s", exc)
    raise RuntimeError(f"MongoDB connection failed: {exc}")


def is_mongodb_active() -> bool:
    """Always returns True since MongoDB is required and verified on startup."""
    return True


# ---------------------------------------------------------------------------
# In-Memory High-Speed Caches (Sub-millisecond access for active operations)
# ---------------------------------------------------------------------------
_credentials_cache: dict[int, dict] = {}
_session_cache: dict[int, dict] = {}
_morning_status_cache: dict[int, bool] = {}
_lms_creds_cache: dict[int, dict] = {}
_lms_token_cache: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Credentials Storage Operations
# ---------------------------------------------------------------------------

def save_user_credentials(user_id: int, username_encrypted: str, password_encrypted: str) -> None:
    """Save encrypted credentials for a user in MongoDB and update in-memory cache."""
    uid = int(user_id)
    _credentials_cache[uid] = {
        "username": username_encrypted,
        "password": password_encrypted,
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
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
    except Exception as exc:
        logger.error("Error saving credentials to MongoDB: %s", exc)
        raise


def load_user_credentials(user_id: int) -> Optional[dict]:
    """Load encrypted credentials record for a specific user with fast in-memory cache lookup."""
    uid = int(user_id)
    if uid in _credentials_cache:
        return dict(_credentials_cache[uid])

    try:
        doc = _mongo_db.users.find_one({"_id": uid})
        if doc and doc.get("username") and doc.get("password"):
            creds = {
                "username": doc["username"],
                "password": doc["password"],
            }
            _credentials_cache[uid] = creds
            return dict(creds)
    except Exception as exc:
        logger.error("Error loading credentials from MongoDB: %s", exc)
    return None


def delete_user_credentials(user_id: int) -> None:
    """Delete a user's stored credentials in MongoDB and memory cache."""
    uid = int(user_id)
    _credentials_cache.pop(uid, None)
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {"$unset": {"username": "", "password": ""}},
        )
    except Exception as exc:
        logger.error("Error deleting credentials in MongoDB: %s", exc)
        raise


def get_all_users() -> dict[str, dict]:
    """Return all stored users with their encrypted credentials from MongoDB."""
    users_dict = {}
    try:
        cursor = _mongo_db.users.find({"username": {"$exists": True, "$ne": ""}})
        for doc in cursor:
            uid_str = str(doc["_id"])
            creds = {
                "username": doc.get("username", ""),
                "password": doc.get("password", ""),
            }
            users_dict[uid_str] = creds
            _credentials_cache[int(doc["_id"])] = creds
    except Exception as exc:
        logger.error("Error fetching all users from MongoDB: %s", exc)
    return users_dict


# ---------------------------------------------------------------------------
# Session Storage Operations (Per-User)
# ---------------------------------------------------------------------------

def save_user_session(user_id: int, cookies: dict, csrf_token: Optional[str] = None) -> None:
    """Save active ERP cookies and CSRF token for a specific Telegram user in MongoDB & memory cache."""
    uid = int(user_id)
    session_data = {
        "cookies": cookies,
        "csrf_token": csrf_token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _session_cache[uid] = session_data

    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {"$set": {"session": session_data}},
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error saving session to MongoDB: %s", exc)
        raise


def load_user_session(user_id: int) -> Optional[dict]:
    """Load active ERP session (cookies and csrf_token) for a user with instant memory cache."""
    uid = int(user_id)
    if uid in _session_cache:
        return dict(_session_cache[uid])

    try:
        doc = _mongo_db.users.find_one({"_id": uid})
        if doc and doc.get("session"):
            sess = doc["session"]
            _session_cache[uid] = sess
            return dict(sess)
    except Exception as exc:
        logger.error("Error loading session from MongoDB: %s", exc)
    return None


def delete_user_session(user_id: int) -> None:
    """Clear session cookies for a user in MongoDB and memory cache."""
    uid = int(user_id)
    _session_cache.pop(uid, None)
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {"$unset": {"session": ""}},
        )
    except Exception as exc:
        logger.error("Error clearing session in MongoDB: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Morning Alert Preferences (Per-User)
# ---------------------------------------------------------------------------

def is_user_morning_enabled(user_id: int) -> bool:
    """Check if morning alert is enabled for a specific user with fast cache lookup."""
    uid = int(user_id)
    if uid in _morning_status_cache:
        return _morning_status_cache[uid]

    try:
        doc = _mongo_db.users.find_one({"_id": uid})
        if doc and "morning_enabled" in doc:
            status = bool(doc["morning_enabled"])
            _morning_status_cache[uid] = status
            return status
    except Exception as exc:
        logger.error("Error reading morning setting from MongoDB: %s", exc)
    return False


def set_user_morning_enabled(user_id: int, enabled: bool) -> None:
    """Toggle morning alert for a specific user in MongoDB and memory cache."""
    uid = int(user_id)
    _morning_status_cache[uid] = bool(enabled)
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {"$set": {"morning_enabled": bool(enabled)}},
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error setting morning preference in MongoDB: %s", exc)
        raise


def get_all_morning_users() -> list[int]:
    """Get all user IDs that have morning alert enabled from MongoDB."""
    user_ids = []
    try:
        cursor = _mongo_db.users.find({"morning_enabled": True})
        for doc in cursor:
            uid = int(doc["_id"])
            user_ids.append(uid)
            _morning_status_cache[uid] = True
    except Exception as exc:
        logger.error("Error fetching morning users from MongoDB: %s", exc)
    return user_ids


def purge_all_database_timetables() -> int:
    """Purge all stored timetables from MongoDB so the bot only uses live ERP data."""
    try:
        res = _mongo_db.users.update_many(
            {"cached_timetable": {"$exists": True}},
            {"$unset": {"cached_timetable": ""}},
        )
        logger.info("Purged cached_timetable from %d MongoDB users", res.modified_count)
        return res.modified_count
    except Exception as exc:
        logger.error("Error purging cached timetables from MongoDB: %s", exc)
        return 0


def delete_user_all_data(user_id: int) -> bool:
    """Completely remove all stored user data (credentials, session, morning settings, LMS) from memory and MongoDB."""
    uid = int(user_id)
    _credentials_cache.pop(uid, None)
    _session_cache.pop(uid, None)
    _morning_status_cache.pop(uid, None)
    _lms_creds_cache.pop(uid, None)
    _lms_token_cache.pop(uid, None)

    try:
        res = _mongo_db.users.delete_one({"_id": uid})
        return res.deleted_count > 0
    except Exception as exc:
        logger.error("Error completely deleting user from MongoDB: %s", exc)
        raise


# ---------------------------------------------------------------------------
# LMS Credentials, Token & Task Tracking (Per-User)
# ---------------------------------------------------------------------------

def save_user_lms_credentials(user_id: int, username_encrypted: str, password_encrypted: str) -> None:
    """Save encrypted LMS credentials for a user in MongoDB and memory cache."""
    uid = int(user_id)
    _lms_creds_cache[uid] = {
        "username": username_encrypted,
        "password": password_encrypted,
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {
                "$set": {
                    "lms_username": username_encrypted,
                    "lms_password": password_encrypted,
                    "lms_updated_at": now_iso,
                },
                "$setOnInsert": {
                    "created_at": now_iso,
                },
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error saving LMS credentials to MongoDB: %s", exc)
        raise


def load_user_lms_credentials(user_id: int) -> Optional[dict]:
    """Load encrypted LMS credentials record for a specific user with fast cache lookup."""
    uid = int(user_id)
    if uid in _lms_creds_cache:
        return dict(_lms_creds_cache[uid])

    try:
        doc = _mongo_db.users.find_one({"_id": uid})
        if doc and doc.get("lms_username") and doc.get("lms_password"):
            creds = {
                "username": doc["lms_username"],
                "password": doc["lms_password"],
            }
            _lms_creds_cache[uid] = creds
            return dict(creds)
    except Exception as exc:
        logger.error("Error loading LMS credentials from MongoDB: %s", exc)
    return None


def delete_user_lms_credentials(user_id: int) -> None:
    """Delete a user's stored LMS credentials, token, and task state in MongoDB and memory cache."""
    uid = int(user_id)
    _lms_creds_cache.pop(uid, None)
    _lms_token_cache.pop(uid, None)
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {"$unset": {"lms_username": "", "lms_password": "", "lms_token": "", "lms_tasks": ""}},
        )
    except Exception as exc:
        logger.error("Error deleting LMS credentials in MongoDB: %s", exc)
        raise


def save_user_lms_token(user_id: int, token: str) -> None:
    """Cache Moodle REST API token for a user in MongoDB and memory cache."""
    uid = int(user_id)
    _lms_token_cache[uid] = token
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _mongo_db.users.update_one(
            {"_id": uid},
            {
                "$set": {
                    "lms_token": token,
                    "lms_token_updated_at": now_iso,
                }
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error saving LMS token in MongoDB: %s", exc)


def load_user_lms_token(user_id: int) -> Optional[str]:
    """Load cached Moodle REST API token for a user from memory cache or MongoDB."""
    uid = int(user_id)
    if uid in _lms_token_cache:
        return _lms_token_cache[uid]

    try:
        doc = _mongo_db.users.find_one({"_id": uid})
        if doc and doc.get("lms_token"):
            token = doc["lms_token"]
            _lms_token_cache[uid] = token
            return token
    except Exception as exc:
        logger.error("Error loading LMS token from MongoDB: %s", exc)
    return None


def get_lms_task_state(user_id: int) -> dict:
    """
    Get user's recorded LMS task state:
    - seen_task_ids: list of assignment/quiz IDs previously seen (used to detect NEW tasks)
    - reminded_48h: list of task IDs that received the 2-day reminder
    - reminded_6h: list of task IDs that received the urgent 6-hour reminder
    """
    try:
        doc = _mongo_db.users.find_one({"_id": int(user_id)})
        if doc and doc.get("lms_tasks"):
            return {
                "seen_task_ids": doc["lms_tasks"].get("seen_task_ids", []),
                "reminded_48h": doc["lms_tasks"].get("reminded_48h", []),
                "reminded_6h": doc["lms_tasks"].get("reminded_6h", []),
            }
    except Exception as exc:
        logger.error("Error loading LMS task state from MongoDB: %s", exc)
    return {"seen_task_ids": [], "reminded_48h": [], "reminded_6h": []}


def update_lms_task_state(
    user_id: int,
    seen_task_ids: list[str],
    reminded_48h: list[str],
    reminded_6h: list[str],
) -> None:
    """Update user's seen and reminded LMS task IDs in MongoDB."""
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _mongo_db.users.update_one(
            {"_id": int(user_id)},
            {
                "$set": {
                    "lms_tasks": {
                        "seen_task_ids": seen_task_ids,
                        "reminded_48h": reminded_48h,
                        "reminded_6h": reminded_6h,
                        "updated_at": now_iso,
                    }
                }
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error updating LMS task state in MongoDB: %s", exc)


def get_all_lms_users() -> list[int]:
    """Get all user IDs that have LMS credentials stored."""
    user_ids = []
    try:
        cursor = _mongo_db.users.find({"lms_username": {"$exists": True, "$ne": ""}})
        for doc in cursor:
            user_ids.append(int(doc["_id"]))
    except Exception as exc:
        logger.error("Error fetching LMS users from MongoDB: %s", exc)
    return user_ids


# ---------------------------------------------------------------------------
# Morning Timetable Cache (Persistent Multi-Tier Cache with MongoDB TTL)
# ---------------------------------------------------------------------------
_morning_cache_index_created = False

def _ensure_morning_cache_index() -> None:
    global _morning_cache_index_created
    if not _morning_cache_index_created:
        try:
            _mongo_db.morning_cache.create_index("expires_at", expireAfterSeconds=0)
            _morning_cache_index_created = True
        except Exception as exc:
            logger.warning("Could not create TTL index on morning_cache: %s", exc)


def save_morning_cache(user_id: int, html_content: str, ttl_seconds: Optional[int] = None) -> None:
    """Save pre-fetched timetable HTML in MongoDB morning_cache collection with TTL."""
    _ensure_morning_cache_index()
    uid = int(user_id)
    if ttl_seconds is None:
        ttl_seconds = getattr(config, "MORNING_CACHE_TTL_SECONDS", 14400)
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, tz=timezone.utc)
    try:
        _mongo_db.morning_cache.update_one(
            {"_id": uid},
            {
                "$set": {
                    "html": html_content,
                    "updated_at": now.isoformat(),
                    "expires_at": expires_at,
                }
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error saving morning timetable cache for user %s: %s", uid, exc)


def load_morning_cache(user_id: int) -> Optional[str]:
    """Load cached timetable HTML from MongoDB morning_cache collection."""
    uid = int(user_id)
    try:
        doc = _mongo_db.morning_cache.find_one({"_id": uid})
        if not doc:
            return None
        expires_at = doc.get("expires_at")
        if expires_at:
            if isinstance(expires_at, datetime):
                # Ensure expires_at is timezone-aware for comparison
                exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
                if exp < datetime.now(timezone.utc):
                    return None
            elif isinstance(expires_at, (int, float)):
                if expires_at < datetime.now(timezone.utc).timestamp():
                    return None
        return doc.get("html")
    except Exception as exc:
        logger.error("Error loading morning timetable cache for user %s: %s", uid, exc)
        return None


def clear_morning_cache() -> None:
    """Clear all records from morning_cache collection."""
    try:
        _mongo_db.morning_cache.delete_many({})
    except Exception as exc:
        logger.error("Error clearing morning timetable cache: %s", exc)


