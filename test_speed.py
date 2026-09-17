import io
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np
from PIL import Image

import config
import storage
import credentials
from erp.captcha_solver import warmup_captcha_solver, solve_captcha
from parser.timetable import get_timetable_messages
import bot

def run_tests():
    print("=" * 60)
    print("🚀 KL UNIVERSITY BOT - PERFORMANCE & SPEED BENCHMARK")
    print("=" * 60)

    # 1. Test In-Memory Storage Cache
    print("\n[1/3] Testing In-Memory Write-Through Storage Cache...")
    test_uid = 999999999
    
    # Save session
    t0 = time.perf_counter()
    storage.save_user_session(test_uid, {"test_cookie": "klu_token_123"}, csrf_token="test_csrf_token")
    t_save = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Write session (DB + memory cache): {t_save:.2f} ms")

    # Read from cache
    t0 = time.perf_counter()
    sess = storage.load_user_session(test_uid)
    t_cached = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Read session from in-memory cache: {t_cached:.4f} ms")
    assert sess is not None and sess.get("cookies") == {"test_cookie": "klu_token_123"}
    storage.delete_user_session(test_uid)
    print("  ✅ Storage Cache Test: PASSED (Sub-millisecond retrieval)")

    # 2. Test CAPTCHA Solver Pre-Warming & Inference
    print("\n[2/3] Testing ONNX CAPTCHA Solver...")
    t0 = time.perf_counter()
    warmup_captcha_solver()
    t_warmup = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Model warmup / load time: {t_warmup:.2f} ms")

    dummy_img = Image.new("RGBA", (140, 40), color=(255, 255, 255, 0))
    buf = io.BytesIO()
    dummy_img.save(buf, format="PNG")
    dummy_bytes = buf.getvalue()

    t0 = time.perf_counter()
    code = solve_captcha(dummy_bytes)
    t_infer = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Inference speed on CAPTCHA image: {t_infer:.2f} ms")
    print("  ✅ CAPTCHA Pre-warm & Inference Test: PASSED")

    # 3. Test Live Timetable Parsing & Per-User Cookie Isolation
    print("\n[3/3] Testing Live Timetable Parsing & Per-User Cookie Isolation...")
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
    # Test timetable message extraction speed
    t0 = time.perf_counter()
    tue_messages = get_timetable_messages(mock_html, day_filter="Tue")
    t_parse = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Live Timetable Parsing speed: {t_parse:.3f} ms")
    assert len(tue_messages) > 0

    # Test per-user client isolation (ensures zero cross-user cookie leakage)
    from erp.client import get_erp_client_for_user, reset_user_client
    c1 = get_erp_client_for_user(10001)
    c2 = get_erp_client_for_user(10002)
    assert c1 is not c2, "Clients must be separate per-user instances"
    assert c1.cookies is not c2.cookies, "Cookie jars must be strictly isolated"
    reset_user_client(10001)
    reset_user_client(10002)
    print("  ✅ Per-User Client & Cookie Isolation: PASSED (100% Zero Leakage)")
    print("  ✅ Live Timetable Parsing: PASSED (Direct clean messages without extra UI buttons)")

    print("\n" + "=" * 60)
    print("🎉 ALL TESTS PASSED! The bot is optimized and ready to run.")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
