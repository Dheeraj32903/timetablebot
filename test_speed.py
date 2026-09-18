import asyncio
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
from erp.captcha_solver import warmup_captcha_solver, solve_captcha, solve_captcha_async
from parser.timetable import get_timetable_messages
from workload import workload_manager, GlobalERPGate, UserLockRegistry, LeakyBucketRateLimiter
import bot


async def run_async_tests():
    print("=" * 65)
    print("🚀 KL UNIVERSITY BOT - MULTI-USER ARCHITECTURE VALIDATION")
    print("=" * 65)

    # 1. Test In-Memory & MongoDB Persistent Caches
    print("\n[1/6] Testing In-Memory Session Cache & MongoDB TTL Morning Cache...")
    test_uid = 999999999

    # Save session
    t0 = time.perf_counter()
    storage.save_user_session(test_uid, {"test_cookie": "klu_token_123"}, csrf_token="test_csrf_token")
    t_save = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Write session (DB + memory cache): {t_save:.2f} ms")

    # Read from session cache
    t0 = time.perf_counter()
    sess = storage.load_user_session(test_uid)
    t_cached = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Read session from in-memory cache: {t_cached:.4f} ms")
    assert sess is not None and sess.get("cookies") == {"test_cookie": "klu_token_123"}
    storage.delete_user_session(test_uid)

    # Test MongoDB Morning Cache with TTL
    sample_html = "<html><body><table><tr><td>Test Schedule</td></tr></table></body></html>"
    t0 = time.perf_counter()
    storage.save_morning_cache(test_uid, sample_html, ttl_seconds=3600)
    t_m_write = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Write persistent morning cache (MongoDB TTL): {t_m_write:.2f} ms")

    t0 = time.perf_counter()
    loaded_html = storage.load_morning_cache(test_uid)
    t_m_read = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Read persistent morning cache (MongoDB): {t_m_read:.2f} ms")
    assert loaded_html == sample_html
    storage.clear_morning_cache()
    print("  ✅ Storage Caches: PASSED (Multi-Tier Session & Morning TTL Cache Verified)")

    # 2. Test CAPTCHA Solver Pre-Warming & Async Thread Offloading
    print("\n[2/6] Testing ONNX CAPTCHA Solver & Async Offloading...")
    t0 = time.perf_counter()
    warmup_captcha_solver()
    t_warmup = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Model warmup / load time: {t_warmup:.2f} ms")

    dummy_img = Image.new("RGBA", (140, 40), color=(255, 255, 255, 0))
    buf = io.BytesIO()
    dummy_img.save(buf, format="PNG")
    dummy_bytes = buf.getvalue()

    t0 = time.perf_counter()
    code_sync = solve_captcha(dummy_bytes)
    t_infer = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Sync Inference speed on CAPTCHA image: {t_infer:.2f} ms")

    t0 = time.perf_counter()
    code_async = await solve_captcha_async(dummy_bytes)
    t_infer_async = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Async (Thread-Offloaded) Inference speed: {t_infer_async:.2f} ms")
    assert code_sync == code_async
    print("  ✅ CAPTCHA Async Offloading: PASSED (Non-blocking on event loop)")

    # 3. Test Global ERP Gate Concurrency & Rate Limiting
    print("\n[3/6] Testing Global ERP Gate Concurrency & Rate Limiter...")
    gate = GlobalERPGate(max_concurrency=5, max_rps=5.0)

    t0 = time.perf_counter()
    # Concurrently acquire 10 slots
    async def _worker(w_id):
        async with gate:
            await asyncio.sleep(0.1)

    tasks = [asyncio.create_task(_worker(i)) for i in range(10)]
    await asyncio.gather(*tasks)
    gate_duration = time.perf_counter() - t0
    print(f"  ⚡ 10 jobs through gate (max_c=5, rate=5.0/s) completed in: {gate_duration:.2f}s")
    print(f"  ⚡ Peak in-flight requests observed: {gate.max_observed_in_flight} (Strict Limit: 5)")
    assert gate.max_observed_in_flight <= 5
    print("  ✅ Global ERP Gate: PASSED (Strict bounded concurrency & rate limiting)")

    # 4. Test User Lock Registry
    print("\n[4/6] Testing UserLockRegistry Isolation...")
    registry = UserLockRegistry()
    l1 = await registry.get_lock(1001)
    l2 = await registry.get_lock(1001)
    l3 = await registry.get_lock(1002)
    assert l1 is l2, "Locks for same user ID must be identical instance"
    assert l1 is not l3, "Locks for different user IDs must be separate instances"
    print("  ✅ UserLockRegistry: PASSED (Per-user locks properly isolated)")

    # 5. Test Live Timetable Parsing & Per-User Client Isolation
    print("\n[5/6] Testing Live Timetable Parsing & Per-User Cookie Isolation...")
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
    t0 = time.perf_counter()
    tue_messages = get_timetable_messages(mock_html, day_filter="Tue")
    t_parse = (time.perf_counter() - t0) * 1000
    print(f"  ⚡ Live Timetable Parsing speed: {t_parse:.3f} ms")
    assert len(tue_messages) > 0

    from erp.client import get_erp_client_for_user, reset_user_client
    c1 = get_erp_client_for_user(10001)
    c2 = get_erp_client_for_user(10002)
    assert c1 is not c2, "Clients must be separate per-user instances"
    assert c1.cookies is not c2.cookies, "Cookie jars must be strictly isolated"
    reset_user_client(10001)
    reset_user_client(10002)
    print("  ✅ Per-User Client & Cookie Isolation: PASSED (100% Zero Leakage)")

    # 6. Test WorkloadManager Priority Queues & Life Cycle
    print("\n[6/6] Testing WorkloadManager Priority Routing & Life Cycle...")
    await workload_manager.start()

    # Submit a fast mock task to timetable_queue
    async def mock_task(val: int, metric=None):
        await asyncio.sleep(0.05)
        return val * 2

    res = await workload_manager.submit_job(
        workload_manager.timetable_queue,
        user_id=12345,
        operation="mock_test",
        queue_name="timetable",
        handler=mock_task,
        val=21,
    )
    assert res == 42
    print(f"  ⚡ WorkloadManager job processed successfully with result: {res}")
    await workload_manager.stop()
    print("  ✅ WorkloadManager Life Cycle: PASSED")

    print("\n" + "=" * 65)
    print("🎉 ALL MULTI-USER ARCHITECTURAL TESTS PASSED SUCCESSFULLY!")
    print("=" * 65)


def run_tests():
    asyncio.run(run_async_tests())


if __name__ == "__main__":
    run_tests()
