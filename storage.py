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
# Credentials Storage Operations
# ---------------------------------------------------------------------------

def save_user_credentials(user_id: int, username_encrypted: str, password_encrypted: str) -> None:
    """Save encrypted credentials for a user in MongoDB."""
    now_iso = datetime.now(timezone.utc).isoformat()
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
    except Exception as exc:
        logger.error("Error saving credentials to MongoDB: %s", exc)
        raise


def load_user_credentials(user_id: int) -> Optional[dict]:
    """Load encrypted credentials record for a specific user from MongoDB."""
    try:
        doc = _mongo_db.users.find_one({"_id": int(user_id)})
        if doc and doc.get("username") and doc.get("password"):
            return {
                "username": doc["username"],
                "password": doc["password"],
            }
    except Exception as exc:
        logger.error("Error loading credentials from MongoDB: %s", exc)
    return None


def delete_user_credentials(user_id: int) -> None:
    """Delete a user's stored credentials in MongoDB."""
    try:
        _mongo_db.users.update_one(
            {"_id": int(user_id)},
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
            uid = str(doc["_id"])
            users_dict[uid] = {
                "username": doc.get("username", ""),
                "password": doc.get("password", ""),
            }
    except Exception as exc:
        logger.error("Error fetching all users from MongoDB: %s", exc)
    return users_dict


# ---------------------------------------------------------------------------
# Session Storage Operations (Per-User)
# ---------------------------------------------------------------------------

def save_user_session(user_id: int, cookies: dict, csrf_token: Optional[str] = None) -> None:
    """Save active ERP cookies and CSRF token for a specific Telegram user in MongoDB."""
    session_data = {
        "cookies": cookies,
        "csrf_token": csrf_token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _mongo_db.users.update_one(
            {"_id": int(user_id)},
            {"$set": {"session": session_data}},
            upsert=True,
        )
    except Exception as exc:
        logger.error("Error saving session to MongoDB: %s", exc)
        raise


def load_user_session(user_id: int) -> Optional[dict]:
    """Load active ERP session (cookies and csrf_token) for a user from MongoDB."""
    try:
        doc = _mongo_db.users.find_one({"_id": int(user_id)})
        if doc and doc.get("session"):
            return doc["session"]
    except Exception as exc:
        logger.error("Error loading session from MongoDB: %s", exc)
    return None


def delete_user_session(user_id: int) -> None:
    """Clear session cookies for a user in MongoDB."""
    try:
        _mongo_db.users.update_one(
            {"_id": int(user_id)},
            {"$unset": {"session": ""}},
        )
    except Exception as exc:
        logger.error("Error clearing session in MongoDB: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Morning Alert Preferences (Per-User)
# ---------------------------------------------------------------------------

def is_user_morning_enabled(user_id: int) -> bool:
    """Check if morning alert is enabled for a specific user in MongoDB."""
    try:
        doc = _mongo_db.users.find_one({"_id": int(user_id)})
        if doc and "morning_enabled" in doc:
            return bool(doc["morning_enabled"])
    except Exception as exc:
        logger.error("Error reading morning setting from MongoDB: %s", exc)
    return False


def set_user_morning_enabled(user_id: int, enabled: bool) -> None:
    """Toggle morning alert for a specific user in MongoDB."""
    try:
        _mongo_db.users.update_one(
            {"_id": int(user_id)},
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
            user_ids.append(int(doc["_id"]))
    except Exception as exc:
        logger.error("Error fetching morning users from MongoDB: %s", exc)
    return user_ids


def delete_user_all_data(user_id: int) -> bool:
    """Completely remove all stored user data (credentials, session, morning settings) from MongoDB."""
    try:
        res = _mongo_db.users.delete_one({"_id": int(user_id)})
        return res.deleted_count > 0
    except Exception as exc:
        logger.error("Error completely deleting user from MongoDB: %s", exc)
        raise
