import json
import logging
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

import config

logger = logging.getLogger(__name__)

SECRET_KEY_FILE = config.PROJECT_ROOT / ".secret.key"


def _get_or_create_encryption_key() -> bytes:
    """
    Retrieve the encryption key from config, .secret.key file, or generate a new one.
    Ensures that credentials are ALWAYS strongly encrypted and never stored in plaintext.
    """
    # 1. Check environment / config variable
    if config.CREDENTIALS_ENCRYPTION_KEY and config.CREDENTIALS_ENCRYPTION_KEY.strip():
        return config.CREDENTIALS_ENCRYPTION_KEY.strip().encode()

    # 2. Check persistent secret key file
    if SECRET_KEY_FILE.exists():
        key_content = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if key_content:
            return key_content.encode()

    # 3. Generate a new Fernet key and persist it
    new_key = Fernet.generate_key()
    SECRET_KEY_FILE.write_text(new_key.decode(), encoding="utf-8")

    # If .env exists, update or append the key for convenience
    env_file = config.PROJECT_ROOT / ".env"
    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            if "CREDENTIALS_ENCRYPTION_KEY=" in content:
                import re
                new_content = re.sub(
                    r"CREDENTIALS_ENCRYPTION_KEY=.*",
                    f"CREDENTIALS_ENCRYPTION_KEY={new_key.decode()}",
                    content,
                )
                env_file.write_text(new_content, encoding="utf-8")
            else:
                env_file.write_text(f"{content}\nCREDENTIALS_ENCRYPTION_KEY={new_key.decode()}\n", encoding="utf-8")
        except Exception:
            logger.warning("Could not update .env with generated encryption key")

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
    # Fernet tokens start with gAAAAA and are base64 strings
    return text.startswith("gAAAAA") and len(text) > 50


def _encrypt_val(value: str) -> str:
    """Encrypt a plaintext string using authenticated AES-128-CBC (Fernet)."""
    if not value:
        return ""
    cipher = _get_fernet()
    return cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_val(value: str) -> str:
    """Decrypt a ciphertext string, or return raw value if unencrypted."""
    if not value or not _is_fernet_token(value):
        return value
    cipher = _get_fernet()
    try:
        return cipher.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        return value


def _load_credentials() -> dict:
    """Load credentials from file, environment variable, or fallback config."""
    import os

    # 1. Check persistent file on disk / injected Render secret file
    if config.CREDENTIALS_FILE.exists():
        try:
            data = json.loads(config.CREDENTIALS_FILE.read_text(encoding="utf-8"))
            if data:
                return data
        except json.JSONDecodeError:
            pass

    # 2. Check environment variable STORED_CREDENTIALS_JSON (Render persistent config)
    env_creds_raw = os.getenv("STORED_CREDENTIALS_JSON", "").strip()
    if env_creds_raw:
        try:
            return json.loads(env_creds_raw)
        except json.JSONDecodeError:
            pass

    return {}


def _save_credentials(creds: dict) -> None:
    config.CREDENTIALS_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")


def save_user_credentials(telegram_user_id: int, username: str, password: str) -> None:
    """
    Store ERP credentials for a Telegram user with strong Fernet encryption.
    Both username and password are encrypted in credentials.json.
    """
    creds = _load_credentials()
    creds[str(telegram_user_id)] = {
        "username": _encrypt_val(username),
        "password": _encrypt_val(password),
        "encrypted": True,
    }
    _save_credentials(creds)


def load_user_credentials(telegram_user_id: int) -> Optional[dict]:
    """Load and decrypt stored ERP credentials for a Telegram user, if any."""
    record = _load_credentials().get(str(telegram_user_id))
    if record:
        return {
            "username": _decrypt_val(record["username"]),
            "password": _decrypt_val(record["password"]),
        }

    # Fallback to ERP_USERNAME and ERP_PASSWORD from .env if configured
    if config.ERP_USERNAME and config.ERP_PASSWORD:
        return {
            "username": config.ERP_USERNAME,
            "password": config.ERP_PASSWORD,
        }

    return None


def delete_user_credentials(telegram_user_id: int) -> None:
    """Remove stored ERP credentials for a Telegram user."""
    creds = _load_credentials()
    if str(telegram_user_id) in creds:
        del creds[str(telegram_user_id)]
        _save_credentials(creds)


def all_user_credentials() -> dict:
    """Return all stored credentials decrypted, keyed by Telegram user ID string."""
    creds = _load_credentials()
    result = {}
    for user_id_str, record in creds.items():
        result[user_id_str] = {
            "username": _decrypt_val(record["username"]),
            "password": _decrypt_val(record["password"]),
        }
    return result


def auto_encrypt_existing_credentials() -> None:
    """
    Inspect credentials.json and immediately encrypt any existing plaintext entries.
    """
    creds = _load_credentials()
    if not creds:
        return

    updated = False
    for user_id_str, record in creds.items():
        if not record.get("encrypted", False) or not _is_fernet_token(record.get("password", "")):
            raw_user = record.get("username", "")
            raw_pass = record.get("password", "")
            creds[user_id_str] = {
                "username": _encrypt_val(raw_user),
                "password": _encrypt_val(raw_pass),
                "encrypted": True,
            }
            updated = True

    if updated:
        _save_credentials(creds)
        logger.info("Migrated existing plaintext credentials in credentials.json to encrypted format.")


# Auto-migrate immediately on module load
auto_encrypt_existing_credentials()
