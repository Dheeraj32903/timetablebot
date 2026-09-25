import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

import config
import storage

logger = logging.getLogger(__name__)


def _get_or_create_encryption_key() -> bytes:
    """
    Retrieve the encryption key with multi-tiered resilience:
    1. Environment variable / config CREDENTIALS_ENCRYPTION_KEY
    2. Persistent secret key file (.secret.key in DATA_DIR)
    3. Stable fallback derived from TELEGRAM_BOT_TOKEN (ensures decryption across redeployments)
    4. Generated Fernet key saved to disk
    """
    # 1. Check environment / config variable
    if config.CREDENTIALS_ENCRYPTION_KEY and config.CREDENTIALS_ENCRYPTION_KEY.strip():
        return config.CREDENTIALS_ENCRYPTION_KEY.strip().encode()

    # 2. Check persistent secret key file
    if config.SECRET_KEY_FILE.exists():
        try:
            key_content = config.SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
            if key_content:
                return key_content.encode()
        except Exception:
            pass

    # 3. Deterministic key derived from TELEGRAM_BOT_TOKEN (redeploy-safe)
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_BOT_TOKEN.strip():
        digest = hashlib.sha256(f"klu-timetable-{config.TELEGRAM_BOT_TOKEN.strip()}".encode()).digest()
        derived_key = base64.urlsafe_b64encode(digest)
        try:
            config.SECRET_KEY_FILE.write_text(derived_key.decode(), encoding="utf-8")
        except Exception:
            pass
        return derived_key

    # 4. Generate a new Fernet key and persist it
    new_key = Fernet.generate_key()
    try:
        config.SECRET_KEY_FILE.write_text(new_key.decode(), encoding="utf-8")
    except Exception:
        pass

    logger.info("Generated a new secure Fernet encryption key.")
    return new_key


_cipher: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _cipher
    if _cipher is None:
        key = _get_or_create_encryption_key()
        _cipher = Fernet(key)
    return _cipher


def _is_fernet_token(text: str) -> bool:
    """Check if a string looks like a base64 Fernet token."""
    if not isinstance(text, str):
        return False
    return text.startswith("gAAAAA") and len(text) > 50


def _encrypt_val(value: str) -> str:
    """Encrypt a plaintext string using authenticated AES-128-CBC (Fernet)."""
    if not value:
        return ""
    cipher = _get_fernet()
    return cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_val(value: str) -> Optional[str]:
    """Decrypt a ciphertext string, or return raw value if unencrypted. Returns None on decrypt failure."""
    if not value:
        return ""
    if not _is_fernet_token(value):
        return value
    cipher = _get_fernet()
    try:
        return cipher.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception) as exc:
        logger.warning("Fernet decryption failed (key mismatch or corruption): %s", exc)
        return None


def save_user_credentials(telegram_user_id: int, username: str, password: str) -> None:
    """
    Store ERP credentials for a Telegram user with strong Fernet encryption in MongoDB/storage.
    """
    enc_user = _encrypt_val(username)
    enc_pass = _encrypt_val(password)
    storage.save_user_credentials(telegram_user_id, enc_user, enc_pass)


def load_user_credentials(telegram_user_id: int) -> Optional[dict]:
    """Load and decrypt stored ERP credentials for a Telegram user from MongoDB/storage."""
    record = storage.load_user_credentials(telegram_user_id)
    if record:
        u = _decrypt_val(record.get("username", ""))
        p = _decrypt_val(record.get("password", ""))
        if u and p:
            return {
                "username": u,
                "password": p,
            }
        logger.warning("Stored ERP credentials for user %s could not be decrypted.", telegram_user_id)
        return None

    # Fallback to ERP_USERNAME and ERP_PASSWORD from .env if configured
    if config.ERP_USERNAME and config.ERP_PASSWORD:
        return {
            "username": config.ERP_USERNAME,
            "password": config.ERP_PASSWORD,
        }

    return None


def delete_user_credentials(telegram_user_id: int) -> None:
    """Remove stored ERP credentials and session for a Telegram user."""
    storage.delete_user_credentials(telegram_user_id)


def all_user_credentials() -> dict:
    """Return all stored credentials decrypted, keyed by Telegram user ID string."""
    raw_users = storage.get_all_users()
    result = {}
    for user_id_str, record in raw_users.items():
        if record.get("username") and record.get("password"):
            u = _decrypt_val(record["username"])
            p = _decrypt_val(record["password"])
            if u and p:
                result[user_id_str] = {
                    "username": u,
                    "password": p,
                }
    return result


def save_user_lms_credentials(telegram_user_id: int, username: str, password: str) -> None:
    """Store LMS credentials for a Telegram user with Fernet encryption in MongoDB."""
    enc_user = _encrypt_val(username)
    enc_pass = _encrypt_val(password)
    storage.save_user_lms_credentials(telegram_user_id, enc_user, enc_pass)


def load_user_lms_credentials(telegram_user_id: int) -> Optional[dict]:
    """Load and decrypt stored LMS credentials for a Telegram user."""
    record = storage.load_user_lms_credentials(telegram_user_id)
    if record:
        u = _decrypt_val(record.get("username", ""))
        p = _decrypt_val(record.get("password", ""))
        if u and p:
            return {
                "username": u,
                "password": p,
            }
        logger.warning("Stored LMS credentials for user %s could not be decrypted.", telegram_user_id)
        return None
    return None


def delete_user_lms_credentials(telegram_user_id: int) -> None:
    """Remove stored LMS credentials and state for a Telegram user."""
    storage.delete_user_lms_credentials(telegram_user_id)


def save_user_lms_token(telegram_user_id: int, token: str) -> None:
    """Store LMS API token encrypted with Fernet in MongoDB."""
    enc_token = _encrypt_val(token)
    storage.save_user_lms_token(telegram_user_id, enc_token)


def load_user_lms_token(telegram_user_id: int) -> Optional[str]:
    """Load and decrypt stored LMS API token from MongoDB."""
    raw = storage.load_user_lms_token(telegram_user_id)
    if raw:
        return _decrypt_val(raw)
    return None



