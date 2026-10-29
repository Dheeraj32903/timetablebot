import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, status
from pydantic import BaseModel

import config
import credentials

logger = logging.getLogger(__name__)


class AuthenticatedUser(BaseModel):
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = "en"
    auth_date: int = 0


def verify_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> AuthenticatedUser:
    """
    Validate Telegram Mini App initData string using standard HMAC-SHA256 procedure.
    Ref: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Telegram initData header (X-Telegram-Init-Data)",
        )

    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid format in Telegram initData",
        ) from exc

    received_hash = parsed_data.pop("hash", None)
    if not received_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing hash signature in initData",
        )

    # 1. Build sorted data_check_string
    sorted_pairs = sorted(parsed_data.items(), key=lambda item: item[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)

    # 2. Derive secret key: HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()

    # 3. Compute expected hash
    expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    # 4. Constant-time comparison
    if not hmac.compare_digest(expected_hash, received_hash):
        logger.warning("Telegram initData HMAC validation failed: hash mismatch")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid cryptographic signature in initData",
        )

    # 5. Check auth_date for replay prevention
    auth_date_str = parsed_data.get("auth_date", "0")
    try:
        auth_date = int(auth_date_str)
    except ValueError:
        auth_date = 0

    if auth_date > 0 and (time.time() - auth_date > max_age_seconds):
        logger.warning("Telegram initData expired (auth_date=%s)", auth_date)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram session expired. Please reopen the Mini App.",
        )

    # 6. Extract user payload
    user_json = parsed_data.get("user")
    if not user_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User information not found in initData",
        )

    try:
        user_dict = json.loads(user_json)
        user_id = int(user_dict.get("id"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to decode user payload from initData",
        ) from exc

    # 7. Authorization check against config.AUTHORIZED_TELEGRAM_USER_IDS
    if config.AUTHORIZED_TELEGRAM_USER_IDS and user_id not in config.AUTHORIZED_TELEGRAM_USER_IDS:
        logger.warning("Unauthorized access attempt by Telegram user %d", user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to access this bot or its dashboard.",
        )

    return AuthenticatedUser(
        id=user_id,
        first_name=user_dict.get("first_name", ""),
        last_name=user_dict.get("last_name", ""),
        username=user_dict.get("username", ""),
        language_code=user_dict.get("language_code", "en"),
        auth_date=auth_date,
    )


async def get_current_user(
    x_telegram_init_data: Optional[str] = Header(None, alias="X-Telegram-Init-Data")
) -> AuthenticatedUser:
    """
    FastAPI dependency that enforces Telegram Mini App authentication.
    """
    # Safe dev/private mobile app bypass if DEV_ALLOW_ANONYMOUS is True in config
    if config.DEV_ALLOW_ANONYMOUS and (not x_telegram_init_data or x_telegram_init_data in ("dev", "app", "")):
        # Prioritize Dheeraj's primary user ID (6986045748) which has both ERP and LMS credentials stored
        primary_uid = 6986045748 if (6986045748 in (config.AUTHORIZED_TELEGRAM_USER_IDS or set())) else (
            next(iter(config.AUTHORIZED_TELEGRAM_USER_IDS)) if config.AUTHORIZED_TELEGRAM_USER_IDS else 123456789
        )
        saved_erp = credentials.load_user_credentials(primary_uid)
        username = (saved_erp or {}).get("username", "2400032903")
        return AuthenticatedUser(
            id=primary_uid,
            first_name="Dheeraj",
            last_name="",
            username=username,
            auth_date=int(time.time()),
        )

    if not x_telegram_init_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: X-Telegram-Init-Data header missing.",
        )

    return verify_telegram_init_data(x_telegram_init_data, config.TELEGRAM_BOT_TOKEN)

