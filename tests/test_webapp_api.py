import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
import credentials
import storage
from webapp.api import app
from webapp.auth import AuthenticatedUser, verify_telegram_init_data


def generate_valid_init_data(user_dict: dict, bot_token: str, auth_date: int = None) -> str:
    """Helper to generate a cryptographically valid Telegram initData query string."""
    if auth_date is None:
        auth_date = int(time.time())

    params = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user_dict, separators=(",", ":")),
    }

    # Sort keys alphabetically
    sorted_pairs = sorted(params.items(), key=lambda x: x[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)

    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    hash_signature = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    params["hash"] = hash_signature
    return urlencode(params)


def test_health_endpoint():
    """Verify health check returns 200 without authentication."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["app"] == "klu-bot-miniapp"


def test_static_frontend_served():
    """Verify frontend static dist index.html is mounted and served."""
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<!doctype html>" in resp.text.lower()
    assert "telegram-web-app.js" in resp.text


def test_telegram_init_data_verification():
    """Test HMAC-SHA256 signature verification logic and authorization enforcement."""
    test_token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
    test_uid = 11223344
    user_data = {
        "id": test_uid,
        "first_name": "Test",
        "last_name": "Student",
        "username": "teststudent",
    }

    orig_auth_ids = set(config.AUTHORIZED_TELEGRAM_USER_IDS)
    config.AUTHORIZED_TELEGRAM_USER_IDS.add(test_uid)

    try:
        # 1. Valid init data with authorized user
        valid_str = generate_valid_init_data(user_data, test_token)
        user = verify_telegram_init_data(valid_str, test_token)
        assert user.id == test_uid
        assert user.first_name == "Test"
        assert user.username == "teststudent"

        # 2. Tampered signature
        tampered_str = valid_str.replace("teststudent", "hacker")
        with pytest.raises(HTTPException) as excinfo:
            verify_telegram_init_data(tampered_str, test_token)
        assert excinfo.value.status_code == 401

        # 3. Expired session (>24h)
        old_date = int(time.time()) - 90000
        expired_str = generate_valid_init_data(user_data, test_token, auth_date=old_date)
        with pytest.raises(HTTPException) as excinfo:
            verify_telegram_init_data(expired_str, test_token)
        assert excinfo.value.status_code == 401

        # 4. Unauthorized user ID
        unauthorized_data = {
            "id": 999111222,
            "first_name": "Intruder",
        }
        unauth_str = generate_valid_init_data(unauthorized_data, test_token)
        with pytest.raises(HTTPException) as excinfo:
            verify_telegram_init_data(unauth_str, test_token)
        assert excinfo.value.status_code == 403

    finally:
        config.AUTHORIZED_TELEGRAM_USER_IDS = orig_auth_ids


def test_api_unauthenticated_rejection():
    """Verify API endpoints strictly reject unauthenticated requests."""
    client = TestClient(app)

    orig_anon = config.DEV_ALLOW_ANONYMOUS
    config.DEV_ALLOW_ANONYMOUS = False
    try:
        resp = client.get("/api/me")
        assert resp.status_code == 401

        resp = client.get("/api/dashboard")
        assert resp.status_code == 401

        resp = client.get("/api/timetable")
        assert resp.status_code == 401
    finally:
        config.DEV_ALLOW_ANONYMOUS = orig_anon


def test_api_me_and_dashboard_with_authenticated_user():
    """Test authenticated profile and consolidated dashboard endpoints."""
    test_token = config.TELEGRAM_BOT_TOKEN or "test_token"
    test_uid = 888888888
    user_data = {
        "id": test_uid,
        "first_name": "Aarav",
        "last_name": "Sharma",
        "username": "aarav_s",
    }
    init_data = generate_valid_init_data(user_data, test_token)

    orig_auth_ids = set(config.AUTHORIZED_TELEGRAM_USER_IDS)
    config.AUTHORIZED_TELEGRAM_USER_IDS.add(test_uid)

    try:
        client = TestClient(app)
        headers = {"X-Telegram-Init-Data": init_data}

        # 1. Profile /api/me
        resp = client.get("/api/me", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == test_uid
        assert data["first_name"] == "Aarav"
        assert "erp" in data
        assert "lms" in data

        # 2. Toggle morning alert /api/settings/morning
        post_resp = client.post("/api/settings/morning", json={"enabled": True}, headers=headers)
        assert post_resp.status_code == 200
        assert post_resp.json()["morning_alerts_enabled"] is True
        assert storage.is_user_morning_enabled(test_uid) is True

        post_resp = client.post("/api/settings/morning", json={"enabled": False}, headers=headers)
        assert post_resp.status_code == 200
        assert post_resp.json()["morning_alerts_enabled"] is False

        # 3. Consolidated dashboard /api/dashboard
        dash_resp = client.get("/api/dashboard", headers=headers)
        assert dash_resp.status_code == 200
        dash_data = dash_resp.json()
        assert dash_data["user"]["id"] == test_uid
        assert "classes_today_count" in dash_data
        assert "pending_tasks_count" in dash_data

        # 4. Outpass allowed times
        outpass_resp = client.get("/api/outpass/allowed-times?out_time=17:00:00", headers=headers)
        assert outpass_resp.status_code == 200
        outpass_data = outpass_resp.json()
        assert outpass_data["out_time"] == "17:00:00"
        assert len(outpass_data["allowed_times"]) > 0

        # 5. Timetable with morning cache
        mock_html = """
        <html>
            <body>
                <table>
                    <tr><th>Day</th><th>1</th><th>2</th></tr>
                    <tr><td>Mon</td><td>24CS3105 - C318</td><td>24AD3107 - C319</td></tr>
                    <tr><td>Tue</td><td>24CI3201 - C320</td><td>24CC3014 - C321</td></tr>
                </table>
            </body>
        </html>
        """
        credentials.save_user_credentials(test_uid, "2400030000", "dummy_pass")
        storage.save_morning_cache(test_uid, mock_html)

        tt_resp = client.get("/api/timetable", headers=headers)
        assert tt_resp.status_code == 200
        tt_data = tt_resp.json()
        assert tt_data["has_timetable"] is True
        assert len(tt_data["days"]) >= 2
        assert any(d["day"] == "Mon" for d in tt_data["days"])

        # Check Dashboard with cached timetable
        dash_resp2 = client.get("/api/dashboard", headers=headers)
        assert dash_resp2.status_code == 200
        assert dash_resp2.json()["user"]["student_id"] == "2400030000"

    finally:
        config.AUTHORIZED_TELEGRAM_USER_IDS = orig_auth_ids
        storage.delete_user_all_data(test_uid)


def test_multi_user_isolation():
    """Verify that User A's data, caches, and preferences are strictly isolated from User B."""
    test_token = config.TELEGRAM_BOT_TOKEN or "test_token"
    uid_a = 777777701
    uid_b = 777777702

    init_a = generate_valid_init_data({"id": uid_a, "first_name": "UserA"}, test_token)
    init_b = generate_valid_init_data({"id": uid_b, "first_name": "UserB"}, test_token)

    orig_auth_ids = set(config.AUTHORIZED_TELEGRAM_USER_IDS)
    config.AUTHORIZED_TELEGRAM_USER_IDS.add(uid_a)
    config.AUTHORIZED_TELEGRAM_USER_IDS.add(uid_b)

    try:
        # Save credentials for User A only
        credentials.save_user_credentials(uid_a, "student_a_2400", "pass_a")
        storage.set_user_morning_enabled(uid_a, True)
        storage.set_user_morning_enabled(uid_b, False)

        client = TestClient(app)

        # User A request
        resp_a = client.get("/api/me", headers={"X-Telegram-Init-Data": init_a})
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert data_a["id"] == uid_a
        assert data_a["student_id"] == "student_a_2400"
        assert data_a["erp"]["connected"] is True
        assert data_a["morning_alerts_enabled"] is True

        # User B request (must NOT see User A's credentials or settings)
        resp_b = client.get("/api/me", headers={"X-Telegram-Init-Data": init_b})
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert data_b["id"] == uid_b
        assert data_b["student_id"] is None
        assert data_b["erp"]["connected"] is False
        assert data_b["morning_alerts_enabled"] is False

    finally:
        config.AUTHORIZED_TELEGRAM_USER_IDS = orig_auth_ids
        storage.delete_user_all_data(uid_a)
        storage.delete_user_all_data(uid_b)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
