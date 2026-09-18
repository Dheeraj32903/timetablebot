import asyncio
import io
import json
import logging
import os
import random
import statistics
import sys
import time
from typing import List, Dict, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import numpy as np
from PIL import Image
import psutil
import httpx

import config
import storage
import credentials
from erp.captcha_solver import warmup_captcha_solver, solve_captcha
from erp.client import get_erp_client_for_user, reset_user_client, auto_login
from erp.session import fetch_timetable_html, load_session, perform_login
from parser.timetable import get_timetable_messages

logging.basicConfig(level=logging.ERROR)

def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p / 100.0)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]

def stats_summary(data: List[float]) -> Dict[str, float]:
    if not data:
        return {"avg": 0, "min": 0, "max": 0, "p50": 0, "p95": 0, "p99": 0}
    return {
        "avg": statistics.mean(data),
        "min": min(data),
        "max": max(data),
        "p50": statistics.median(data),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
    }

async def main():
    print("=" * 75, flush=True)
    print("🔍 KL UNIVERSITY BOT - REALISTIC PERFORMANCE BENCHMARK SUITE", flush=True)
    print("=" * 75, flush=True)

    results = {}
    proc = psutil.Process(os.getpid())
    initial_mem_mb = proc.memory_info().rss / (1024 * 1024)

    # ---------------------------------------------------------------------
    # 1. MongoDB Cache Read / Write / TTL Latency Benchmark
    # ---------------------------------------------------------------------
    print("\n[1/8] Benchmarking MongoDB Cache Latency (Real Atlas Cluster)...", flush=True)
    sample_payload = "<html><body><table>" + "".join([f"<tr><td>Day {i}</td><td>Course 24CS{3100+i} - Room C{100+i}</td></tr>" for i in range(80)]) + "</table></body></html>"
    payload_bytes = len(sample_payload.encode("utf-8"))
    print(f"  • Test HTML payload size: {payload_bytes / 1024:.2f} KB", flush=True)

    db = storage._mongo_db
    cache_col = db["benchmark_morning_cache"]
    cache_col.create_index("expires_at", expireAfterSeconds=0)

    write_times = []
    read_times = []

    for i in range(25):
        test_id = 91000000 + i
        t0 = time.perf_counter()
        cache_col.update_one(
            {"_id": test_id},
            {"$set": {"html": sample_payload, "updated_at": time.time(), "expires_at": time.time() + 3600}},
            upsert=True
        )
        t_write = (time.perf_counter() - t0) * 1000.0
        write_times.append(t_write)

        t0 = time.perf_counter()
        doc = cache_col.find_one({"_id": test_id})
        t_read = (time.perf_counter() - t0) * 1000.0
        read_times.append(t_read)

    cache_col.drop()

    results["mongodb_write_ms"] = stats_summary(write_times)
    results["mongodb_read_ms"] = stats_summary(read_times)
    print(f"  ⚡ MongoDB Write Latency: Avg={results['mongodb_write_ms']['avg']:.2f}ms, P95={results['mongodb_write_ms']['p95']:.2f}ms, P99={results['mongodb_write_ms']['p99']:.2f}ms", flush=True)
    print(f"  ⚡ MongoDB Read Latency:  Avg={results['mongodb_read_ms']['avg']:.2f}ms, P95={results['mongodb_read_ms']['p95']:.2f}ms, P99={results['mongodb_read_ms']['p99']:.2f}ms", flush=True)

    # ---------------------------------------------------------------------
    # 2. ONNX CAPTCHA Solver Latency & Resource Consumption
    # ---------------------------------------------------------------------
    print("\n[2/8] Benchmarking ONNX CAPTCHA Solver (CRNN inference)...", flush=True)
    t0 = time.perf_counter()
    warmup_captcha_solver()
    warmup_ms = (time.perf_counter() - t0) * 1000.0
    print(f"  • Model Warmup Time: {warmup_ms:.2f}ms", flush=True)

    dummy_imgs = []
    for _ in range(40):
        img = Image.new("RGBA", (140, 40), color=(random.randint(0, 50), random.randint(0, 50), random.randint(0, 50), 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        dummy_imgs.append(buf.getvalue())

    captcha_times = []
    mem_before_cap = proc.memory_info().rss / (1024 * 1024)

    for b in dummy_imgs:
        t0 = time.perf_counter()
        code = solve_captcha(b)
        t_cap = (time.perf_counter() - t0) * 1000.0
        captcha_times.append(t_cap)

    mem_after_cap = proc.memory_info().rss / (1024 * 1024)
    results["captcha_single_ms"] = stats_summary(captcha_times)
    results["captcha_mem_overhead_mb"] = mem_after_cap - mem_before_cap

    print(f"  ⚡ Single-threaded CAPTCHA: Avg={results['captcha_single_ms']['avg']:.2f}ms, P95={results['captcha_single_ms']['p95']:.2f}ms, P99={results['captcha_single_ms']['p99']:.2f}ms", flush=True)
    print(f"  ⚡ Memory delta: {results['captcha_mem_overhead_mb']:.2f} MB (Working Set: {mem_after_cap:.1f} MB)", flush=True)

    # Concurrency test
    t0 = time.perf_counter()
    tasks = [asyncio.to_thread(solve_captcha, dummy_imgs[i % len(dummy_imgs)]) for i in range(24)]
    await asyncio.gather(*tasks)
    total_conc_time = (time.perf_counter() - t0) * 1000.0
    results["captcha_concurrent_throughput_rps"] = 24.0 / (total_conc_time / 1000.0)
    print(f"  ⚡ 24 Concurrent Solves via Thread Pool: {total_conc_time:.1f}ms ({results['captcha_concurrent_throughput_rps']:.1f} solves/sec)", flush=True)

    # ---------------------------------------------------------------------
    # 3. Live ERP Login + CAPTCHA Re-auth
    # ---------------------------------------------------------------------
    print("\n[3/8] Benchmarking Live ERP Login + CAPTCHA Re-auth...", flush=True)
    users = storage.get_all_users()
    live_uid = int(list(users.keys())[0])
    creds = credentials.load_user_credentials(live_uid)
    login_times = []

    if creds and creds.get("username") and creds.get("password"):
        for l_idx in range(3):
            reset_user_client(live_uid)
            t0 = time.perf_counter()
            try:
                res = await auto_login(creds["username"], creds["password"], user_id=live_uid)
                dur_ms = (time.perf_counter() - t0) * 1000.0
                if res and res.get("cookies"):
                    login_times.append(dur_ms)
                    print(f"  • Login #{l_idx+1}: {dur_ms:.1f}ms (CSRF: {res.get('csrf_token')[:8]}...)", flush=True)
            except Exception as exc:
                print(f"  • Login #{l_idx+1} error: {exc}", flush=True)
            await asyncio.sleep(1.2)

    results["live_login_ms"] = stats_summary(login_times)
    if login_times:
        print(f"  ⚡ Live Login + CAPTCHA Latency: Avg={results['live_login_ms']['avg']:.1f}ms, P95={results['live_login_ms']['p95']:.1f}ms, P99={results['live_login_ms']['p99']:.1f}ms", flush=True)

    # ---------------------------------------------------------------------
    # 4. Live ERP Timetable Fetch Latency (Valid Session)
    # ---------------------------------------------------------------------
    print("\n[4/8] Benchmarking Live ERP Timetable Fetch with Valid Session...", flush=True)
    live_tt_times = []
    parse_times = []

    for attempt in range(4):
        t0 = time.perf_counter()
        try:
            html = await fetch_timetable_html(user_id=live_uid)
            dur_ms = (time.perf_counter() - t0) * 1000.0
            if html and len(html) > 500:
                live_tt_times.append(dur_ms)
                html_size_kb = len(html.encode("utf-8")) / 1024.0

                t_p0 = time.perf_counter()
                msgs = get_timetable_messages(html, day_filter="Mon")
                p_dur = (time.perf_counter() - t_p0) * 1000.0
                parse_times.append(p_dur)
                print(f"  • Fetch #{attempt+1}: {dur_ms:.1f}ms (Response: {html_size_kb:.1f} KB, Parse: {p_dur:.2f}ms)", flush=True)
        except Exception as e:
            print(f"  • Fetch #{attempt+1} error: {e}", flush=True)
        await asyncio.sleep(1.0)

    results["live_timetable_fetch_ms"] = stats_summary(live_tt_times)
    results["timetable_parse_ms"] = stats_summary(parse_times)
    print(f"  ⚡ Live Timetable Fetch (Valid Session): Avg={results['live_timetable_fetch_ms']['avg']:.1f}ms, P95={results['live_timetable_fetch_ms']['p95']:.1f}ms, P99={results['live_timetable_fetch_ms']['p99']:.1f}ms", flush=True)
    print(f"  ⚡ HTML Parse Time: Avg={results['timetable_parse_ms']['avg']:.2f}ms, P95={results['timetable_parse_ms']['p95']:.2f}ms", flush=True)

    # ---------------------------------------------------------------------
    # 5. ERP Response Latency Under Controlled Concurrency (1, 2, 3, 5, 8)
    # ---------------------------------------------------------------------
    print("\n[5/8] Probing ERP Response Latency vs. Concurrency Level...", flush=True)

    async def probe_concurrency(level: int):
        async with httpx.AsyncClient(timeout=15.0, verify=False, follow_redirects=True) as client:
            latencies = []
            errors = 0
            t0 = time.perf_counter()

            async def single_probe():
                nonlocal errors
                p_t0 = time.perf_counter()
                try:
                    r = await client.get(config.ERP_LOGIN_URL)
                    if r.status_code == 200:
                        latencies.append((time.perf_counter() - p_t0) * 1000.0)
                    else:
                        errors += 1
                except Exception as exc:
                    errors += 1

            tasks = [single_probe() for _ in range(level)]
            await asyncio.gather(*tasks)
            wall_clock = (time.perf_counter() - t0) * 1000.0
            return latencies, errors, wall_clock

    results["concurrency_probes"] = {}
    for c in [1, 2, 3, 5, 8]:
        lats, errs, wall = await probe_concurrency(c)
        s = stats_summary(lats)
        s["errors"] = errs
        s["wall_clock_ms"] = wall
        s["effective_rps"] = (c - errs) / (wall / 1000.0) if wall > 0 else 0
        results["concurrency_probes"][c] = s
        print(f"  • Concurrency={c}: Avg={s['avg']:.1f}ms, P95={s['p95']:.1f}ms, Wall={wall:.1f}ms, Errors={errs}, Eff RPS={s['effective_rps']:.2f}", flush=True)
        await asyncio.sleep(1.0)

    # ---------------------------------------------------------------------
    # 6. Telegram Outbox Dispatcher & 429 Retry-After Handling
    # ---------------------------------------------------------------------
    print("\n[6/8] Benchmarking Telegram Outbox Dispatcher & 429 Retry-After...", flush=True)

    class SimulatedTelegramBot:
        def __init__(self, fail_at_items=[12, 35], retry_after=0.75):
            self.sent = 0
            self.fail_targets = set(fail_at_items)
            self.retry_after = retry_after
            self.retried = 0

        async def send_message(self, chat_id, text):
            self.sent += 1
            if self.sent in self.fail_targets:
                self.fail_targets.remove(self.sent)
                self.retried += 1
                raise Exception(f"429: Too Many Requests: retry after {self.retry_after}")
            await asyncio.sleep(0.015)

    bot = SimulatedTelegramBot(fail_at_items=[12, 35], retry_after=0.75)
    queue = asyncio.Queue()
    num_messages = 50
    for i in range(num_messages):
        await queue.put((100000 + i, f"Morning Timetable Message #{i}"))

    t0 = time.perf_counter()
    target_rps = 20.0
    min_interval = 1.0 / target_rps
    last_send = 0.0
    delivered = 0

    while not queue.empty():
        item = await queue.get()
        chat_id, text = item

        now = time.monotonic()
        if now - last_send < min_interval:
            await asyncio.sleep(min_interval - (now - last_send))

        try:
            await bot.send_message(chat_id, text)
            last_send = time.monotonic()
            delivered += 1
            queue.task_done()
        except Exception as e:
            if "retry after" in str(e).lower():
                await queue.put(item)
                queue.task_done()
                await asyncio.sleep(0.75 + 0.05)

    outbox_dur = time.perf_counter() - t0
    results["outbox_delivered"] = delivered
    results["outbox_duration_s"] = outbox_dur
    results["outbox_rps"] = delivered / outbox_dur
    print(f"  ⚡ Delivered {delivered} messages with {bot.retried} simulated 429 retries: {outbox_dur:.2f}s ({results['outbox_rps']:.1f} msg/sec)", flush=True)

    # ---------------------------------------------------------------------
    # 7. Discrete-Event Morning Prefetch Simulation (100, 500, 1000 users)
    # ---------------------------------------------------------------------
    print("\n[7/8] Simulating Morning Prefetch using Measured Statistical Latencies...", flush=True)

    tt_mean = results["live_timetable_fetch_ms"]["avg"] / 1000.0 if results["live_timetable_fetch_ms"]["avg"] > 0 else 1.2
    login_mean = results["live_login_ms"]["avg"] / 1000.0 if results["live_login_ms"]["avg"] > 0 else 1.6

    print(f"  • Using Empirical Latencies: TT fetch = {tt_mean:.2f}s, Login+CAPTCHA = {login_mean:.2f}s", flush=True)

    def run_discrete_event_simulation(num_users: int, num_workers: int, max_erp_concurrency: int):
        import heapq
        events = []
        user_tasks = []
        for uid in range(num_users):
            is_expired = (random.random() < 0.20)
            dur = random.gauss(login_mean + tt_mean, 0.25) if is_expired else random.gauss(tt_mean, 0.12)
            dur = max(0.4, dur)
            user_tasks.append((uid, dur, is_expired))

        sim_time = 0.0
        task_queue = list(user_tasks)
        active_erp = 0
        worker_available = [True] * num_workers
        user_latencies = []
        queue_wait_times = []

        def schedule_next():
            nonlocal sim_time, active_erp
            for w_id in range(num_workers):
                if worker_available[w_id] and active_erp < max_erp_concurrency and task_queue:
                    uid, dur, is_exp = task_queue.pop(0)
                    worker_available[w_id] = False
                    active_erp += 1
                    queue_wait_times.append(sim_time)
                    heapq.heappush(events, (sim_time + dur, "finish", w_id, uid, dur))

        schedule_next()

        while events:
            evt = heapq.heappop(events)
            sim_time, evt_type, w_id, uid, dur = evt
            worker_available[w_id] = True
            active_erp -= 1
            user_latencies.append(dur)
            schedule_next()

        return sim_time, user_latencies, queue_wait_times

    results["scale_simulations"] = {}
    for n in [100, 500, 1000]:
        sim_wall, u_lats, q_waits = run_discrete_event_simulation(n, num_workers=4, max_erp_concurrency=5)
        results["scale_simulations"][n] = {
            "completion_time_sec": sim_wall,
            "completion_time_min": sim_wall / 60.0,
            "user_duration": stats_summary([x * 1000.0 for x in u_lats]),
            "queue_wait": stats_summary([x * 1000.0 for x in q_waits]),
        }
        print(f"  • {n:4d} Users Prefetch: {sim_wall:6.1f}s ({sim_wall/60.0:4.2f} mins) | P95 Latency={results['scale_simulations'][n]['user_duration']['p95']/1000:4.2f}s | P95 Queue Wait={results['scale_simulations'][n]['queue_wait']['p95']/1000:4.2f}s", flush=True)

    # ---------------------------------------------------------------------
    # 8. Overall Resource & Process Profile
    # ---------------------------------------------------------------------
    print("\n[8/8] System Resource Profile Summary...", flush=True)
    final_mem_mb = proc.memory_info().rss / (1024 * 1024)
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_cores = psutil.cpu_count(logical=True)

    results["system_profile"] = {
        "cpu_cores": cpu_cores,
        "cpu_percent": cpu_percent,
        "initial_mem_mb": initial_mem_mb,
        "final_mem_mb": final_mem_mb,
        "net_mem_delta_mb": final_mem_mb - initial_mem_mb,
    }

    print(f"  • CPU Cores: {cpu_cores} (Current Load: {cpu_percent}%)", flush=True)
    print(f"  • Initial Memory: {initial_mem_mb:.1f} MB ➔ Peak Memory: {final_mem_mb:.1f} MB (Delta: +{final_mem_mb - initial_mem_mb:.1f} MB)", flush=True)

    print("\n" + "=" * 75, flush=True)
    print("✅ BENCHMARK SUITE FINISHED SUCCESSFULLY", flush=True)
    print("=" * 75, flush=True)

    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved metrics to benchmark_results.json", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
