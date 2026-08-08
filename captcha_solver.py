import base64
import time
from typing import Optional

import requests

import config


def _download_image_as_base64(url: str, cookies: dict, headers: dict) -> str:
    """Download an image using the ERP session and return it as base64."""
    response = requests.get(url, headers=headers, cookies=cookies, timeout=30)
    response.raise_for_status()
    return base64.b64encode(response.content).decode("utf-8")


def _solve_with_2captcha(image_base64: str, timeout_seconds: int) -> str:
    """Submit a base64 image to 2captcha and poll for the result."""
    if not config.CAPTCHA_API_KEY:
        raise RuntimeError("CAPTCHA_API_KEY is not set in .env")

    # Submit the CAPTCHA image.
    submit_payload = {
        "key": config.CAPTCHA_API_KEY,
        "method": "base64",
        "body": image_base64,
        "json": 1,
    }
    submit_response = requests.post(
        "https://2captcha.com/in.php",
        data=submit_payload,
        timeout=60,
    )
    submit_response.raise_for_status()
    result = submit_response.json()

    if result.get("status") != 1:
        raise RuntimeError(f"2captcha submission failed: {result}")

    captcha_id = result.get("request")
    if not captcha_id:
        raise RuntimeError("2captcha did not return a captcha ID")

    # Poll for the result.
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        poll_response = requests.get(
            "https://2captcha.com/res.php",
            params={
                "key": config.CAPTCHA_API_KEY,
                "action": "get",
                "id": captcha_id,
                "json": 1,
            },
            timeout=60,
        )
        poll_response.raise_for_status()
        poll_result = poll_response.json()

        if poll_result.get("status") == 1:
            return str(poll_result.get("request", ""))

        if poll_result.get("request") == "CAPCHA_NOT_READY":
            time.sleep(5)
            continue

        raise RuntimeError(f"2captcha polling failed: {poll_result}")

    raise RuntimeError("2captcha solving timed out")


def solve_captcha_image(image_url: str, cookies: dict, headers: dict) -> str:
    """
    Solve a CAPTCHA image using the configured service.
    Currently supports 2captcha.
    """
    service = config.CAPTCHA_SERVICE.lower().strip()
    if service not in {"2captcha"}:
        raise RuntimeError(f"Unsupported CAPTCHA_SERVICE: {config.CAPTCHA_SERVICE}")

    image_base64 = _download_image_as_base64(image_url, cookies, headers)

    if service == "2captcha":
        return _solve_with_2captcha(image_base64, config.CAPTCHA_SOLVE_TIMEOUT_SECONDS)

    raise RuntimeError("No CAPTCHA solver configured")
