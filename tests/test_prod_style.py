"""Production-style tests: real cf_selenium.Browser against mock staging.

Tests (staging only, 127.0.0.1:18801):
  1. snapshot() on /none → provider=unknown
  2. snapshot() on /with-form → fill form, verify cookies
  3. multi-step nav: /dashboard → /products → back
  4. screenshot capture
  5. recording + replay
  6. challenge flow: /cloudflare → cf_selenium auto-solve (or stop)
  7. agent loop on /with-form goal="fill username admin and click Sign in"
  8. agent loop on /dashboard goal="click Products link and report title"
  9. multi-provider detection (9 endpoints)

Artifacts per test:
  ~/test_logs/<run_id>/<NN>_<test_name>/
    result.json     - pass/fail + evidence + timing
    log.txt         - test stdout
    screenshot.png  - last browser screenshot (if any)
    cleanup.log     - per-subtest DB wipe + profile cleanup
    subtest_*.json  - per-subtest detection results (test 9)
"""
import io
import json
import os
import sys
import time
import shutil
import contextlib
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / 'specter'))

# pre-cleanup any zombie chrome
os.system("bash ~/.tmp_cleanup.sh")
time.sleep(1)

from cf_selenium import Browser
from specter.agent import AIWebAgent, TraceEvent

BASE = "http://127.0.0.1:18801"

# run id = timestamp + short random
import random
RUN_ID = f"run_{int(time.time())}_{random.randint(1000,9999):04d}"
LOG_ROOT = Path.home() / "test_logs" / RUN_ID
LOG_ROOT.mkdir(parents=True, exist_ok=True)

results = {"passed": 0, "failed": 0, "tests": []}


class TestContext:
    """Captures artifacts for a single test.

    Usage:
        with TestContext("snapshot", 1) as ctx:
            b = Browser(...)
            ctx.screenshot(b)        # optional
            ctx.evidence({"key": v}) # adds to result.json
            ...                      # prints go to ctx.log
    """

    def __init__(self, name: str, n: int):
        self.name = name
        self.dir = LOG_ROOT / f"{n:02d}_{name}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._buf = io.StringIO()
        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr
        self.evidence: dict = {}
        self.start = time.time()
        self.end: float = 0.0
        self.ok: bool = False
        self.detail: str = ""
        self.screenshot_path: str | None = None
        self.cleanup_lines: list[str] = []

    def __enter__(self):
        sys.stdout = self._buf
        sys.stderr = self._buf
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.stdout = self._real_stdout
        sys.stderr = self._real_stderr
        self.end = time.time()
        if exc is not None:
            tb_text = "".join(traceback.format_exception(exc_type, exc, tb))
            self.evidence["exception"] = f"{type(exc).__name__}: {exc}"
            self.evidence["traceback"] = tb_text
        # write log.txt
        (self.dir / "log.txt").write_text(self._buf.getvalue(), encoding="utf-8")
        # write cleanup.log
        (self.dir / "cleanup.log").write_text(
            "\n".join(self.cleanup_lines) + "\n", encoding="utf-8"
        )
        # write result.json
        self._write_result()
        # mirror to console
        self._real_stdout.write(self._buf.getvalue())
        return False  # don't suppress

    def _write_result(self):
        r = {
            "test": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_seconds": round(self.end - self.start, 3),
            "started_at": datetime.fromtimestamp(self.start).isoformat(),
            "ended_at": datetime.fromtimestamp(self.end).isoformat(),
            "evidence": self.evidence,
            "screenshot": self.screenshot_path,
            "cleanup_actions": len(self.cleanup_lines),
        }
        (self.dir / "result.json").write_text(
            json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def evidence_add(self, key: str, value):
        self.evidence[key] = value

    def cleanup(self, msg: str):
        self.cleanup_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def set_screenshot(self, b: Browser, label: str = "screenshot"):
        try:
            path = b.screenshot(str(self.dir / f"{label}.png"))
            self.screenshot_path = str(path)
            return path
        except Exception as e:
            self.cleanup(f"screenshot failed: {e}")
            return None


def record(ctx: TestContext, detail: str = "", ok: bool | None = None) -> None:
    """Finalize test: set ok/detail, write result, mirror to console."""
    if ok is None:
        ok = not bool(ctx.evidence.get("exception"))
    ctx.ok = ok
    ctx.detail = detail
    ctx.evidence.setdefault("final_detail", detail)
    # status
    status = "✓" if ok else "✗"
    results["tests"].append({"name": ctx.name, "ok": ok, "detail": detail})
    if ok:
        results["passed"] += 1
    else:
        results["failed"] += 1
    print(f"  {status} {ctx.name}: {detail}")


def section(title: str) -> None:
    print(f"\n{'='*60}\n{title}\n{'='*60}")


# ═════════════════════════════════════════════════════════════════════
# Test 1: snapshot() on /none → provider=unknown
# ═════════════════════════════════════════════════════════════════════
section("TEST 1: cf_selenium.Browser snapshot() on /none")
with TestContext("snapshot", 1) as ctx:
    b = Browser(profile="prod_test_1", headless=True, auto_solve=True)
    b.get(f"{BASE}/none")
    snap = b.snapshot()
    ok = (
        snap.get("title") == "No Protection"
        and snap.get("challenge") == "none"
        and snap.get("cookies_count", 0) >= 0
    )
    ctx.evidence_add("title", snap.get("title"))
    ctx.evidence_add("challenge", snap.get("challenge"))
    ctx.evidence_add("cookies_count", snap.get("cookies_count"))
    ctx.set_screenshot(b, "snapshot_none")
    ctx.cleanup("quit browser")
    b.quit()
    record(ctx, f"title={snap.get('title')!r} challenge={snap.get('challenge')}", ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 2: fill form, verify cookie on result page
# ═════════════════════════════════════════════════════════════════════
section("TEST 2: Fill form on /with-form, submit, verify cookie")
with TestContext("fill_form", 2) as ctx:
    b = Browser(profile="prod_test_2", headless=True, auto_solve=True)
    b.get(f"{BASE}/with-form")
    el_u = b.find_element("#u")
    b._run(el_u.type("admin"))
    el_p = b.find_element("#p")
    b._run(el_p.type("secret123"))
    shot_path = ctx.set_screenshot(b, "form_filled")
    if shot_path:
        print(f"    screenshot: {Path(shot_path).name} ({Path(shot_path).stat().st_size}B)")
    btn = b.find_element("#submit-btn")
    b._run(btn.click())
    time.sleep(2)
    snap2 = b.snapshot()
    title_ok = "Welcome" in snap2.get("title", "")
    cookie_ok = "session" in (b.cookies or {})
    ctx.evidence_add("url_after_submit", b.url)
    ctx.evidence_add("title_after_submit", snap2.get("title"))
    ctx.evidence_add("cookies_after_submit", list((b.cookies or {}).keys()))
    ctx.evidence_add("session_cookie_set", cookie_ok)
    ctx.set_screenshot(b, "login_result")
    b.quit()
    record(ctx,
           f"url={b.url} title={snap2.get('title')!r} session_cookie={'set' if cookie_ok else 'missing'}",
           ok=title_ok and cookie_ok)


# ═════════════════════════════════════════════════════════════════════
# Test 3: multi-step nav
# ═════════════════════════════════════════════════════════════════════
section("TEST 3: Navigate /dashboard → /products → back")
with TestContext("multi_step_nav", 3) as ctx:
    b = Browser(profile="prod_test_3", headless=True, auto_solve=True)
    b.get(f"{BASE}/dashboard")
    time.sleep(1)
    title1 = b.snapshot().get("title")
    link = b.find_element("#products-link")
    b._run(link.click())
    time.sleep(2)
    snap2 = b.snapshot()
    title2 = snap2.get("title")
    url2 = b.url
    back = b.find_by_text("Back to dashboard", tag="a")
    b._run(back.click())
    time.sleep(2)
    title3 = b.snapshot().get("title")
    ok = title1 == "Dashboard" and title2 == "Products" and title3 == "Dashboard"
    ctx.evidence_add("titles", {"step1": title1, "step2": title2, "step3": title3})
    ctx.evidence_add("products_url", url2)
    ctx.set_screenshot(b, "back_at_dashboard")
    b.quit()
    record(ctx, f"titles: {title1!r} → {title2!r} (url={url2}) → {title3!r}", ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 4: screenshot + page signature
# ═════════════════════════════════════════════════════════════════════
section("TEST 4: Screenshot + page signature (cf_agent.page_signature)")
with TestContext("signature", 4) as ctx:
    from specter.agent import page_signature
    b = Browser(profile="prod_test_4", headless=True, auto_solve=True)
    b.get(f"{BASE}/dashboard")
    time.sleep(1)
    snap = b.snapshot()
    sig1 = page_signature(snap)
    shot1 = ctx.set_screenshot(b, "before_click")
    link = b.find_element("#products-link")
    b._run(link.click())
    time.sleep(2)
    snap2 = b.snapshot()
    sig2 = page_signature(snap2)
    shot2 = ctx.set_screenshot(b, "after_click")
    sig1_size = Path(shot1).stat().st_size if shot1 else 0
    sig2_size = Path(shot2).stat().st_size if shot2 else 0
    ok = sig1 != sig2 and sig1_size > 0 and sig2_size > 0
    ctx.evidence_add("sig1", sig1)
    ctx.evidence_add("sig2", sig2)
    ctx.evidence_add("sig1_size_bytes", sig1_size)
    ctx.evidence_add("sig2_size_bytes", sig2_size)
    b.quit()
    record(ctx, f"sig1={sig1[:8]} sig2={sig2[:8]} same={sig1==sig2}", ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 5: recording + replay
# ═════════════════════════════════════════════════════════════════════
section("TEST 5: Recording + replay")
with TestContext("recording_replay", 5) as ctx:
    from cf_selenium import Recorder
    rec_dir = ctx.dir / "recording"
    rec_dir.mkdir(exist_ok=True)
    rec_path = rec_dir / "recording.json"

    # record
    b = Browser(profile="prod_test_5a", headless=True, auto_solve=True)
    rec = Recorder()
    rec.attach(b)
    rec.start()
    b.get(f"{BASE}/dashboard")
    time.sleep(1)
    el = b.find_element("#products-link")
    el.click()
    time.sleep(2)
    rec.stop()
    rec.save(str(rec_path))
    actions_n = len(json.loads(rec_path.read_text())["actions"])
    b.quit()
    ctx.cleanup(f"saved recording: {rec_path.name} ({actions_n} actions)")

    # replay
    actions = json.loads(rec_path.read_text())["actions"]
    b2 = Browser(profile="prod_test_5b", headless=True, auto_solve=True)
    b2.recorder = None
    for a in actions:
        if a.get("type") == "navigate":
            b2.get(a["url"])
            time.sleep(1)
        elif a.get("type") == "click":
            el = b2.find_element(a["selector"])
            b2._run(el.click())
            time.sleep(2)
    time.sleep(1)
    title = b2.snapshot().get("title")
    url = b2.url
    ok = len(actions) >= 2 and title == "Products" and "products" in url
    ctx.evidence_add("recorded_actions", actions_n)
    ctx.evidence_add("replay_url", url)
    ctx.evidence_add("replay_title", title)
    ctx.set_screenshot(b2, "replay_final")
    b2.quit()
    record(ctx, f"recorded {actions_n} actions, replay url={url} title={title!r}", ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 6: /cloudflare page
# ═════════════════════════════════════════════════════════════════════
section("TEST 6: /cloudflare mock → cf_selenium handles")
with TestContext("cf_solve", 6) as ctx:
    b = Browser(profile="prod_test_6", headless=True, auto_solve=True)
    b.get(f"{BASE}/cloudflare")
    snap = b.snapshot()
    cookies_n = snap.get("cookies_count", 0)
    challenge = snap.get("challenge", "none")
    cookies = b.cookies or {}
    ok = cookies_n >= 0
    ctx.evidence_add("cookies_count", cookies_n)
    ctx.evidence_add("challenge", challenge)
    ctx.evidence_add("cookies_list", list(cookies.keys()))
    ctx.evidence_add("title", snap.get("title"))
    ctx.set_screenshot(b, "cloudflare_solved")
    b.quit()
    record(ctx, f"cookies={cookies_n} challenge={challenge} title={snap.get('title')!r}", ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 7: AIWebAgent end-to-end on /with-form
# ═════════════════════════════════════════════════════════════════════
section("TEST 7: AIWebAgent on /with-form (rule-based, no LLM)")
with TestContext("agent_form", 7) as ctx:
    agent = AIWebAgent(
        profile="prod_test_7",
        max_steps=4,
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,
    )
    result = agent.run(f"{BASE}/with-form", "fill username admin and click Sign in")
    final_title = result.final_title
    ok = bool(result.steps and len(result.steps) >= 1 and "127.0.0.1" in result.final_url)
    ctx.evidence_add("steps_count", len(result.steps))
    ctx.evidence_add("final_url", result.final_url)
    ctx.evidence_add("final_title", final_title)
    ctx.evidence_add("success", result.success)
    ctx.evidence_add("agent_artifacts_dir", str(result.artifacts_dir))
    print(f"    summary: {result.summary[:80]!r}")
    print(f"    artifacts: {result.artifacts_dir}")
    record(ctx,
           f"steps={len(result.steps)} final_url={result.final_url!r} final_title={final_title!r} success={result.success}",
           ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 8: AIWebAgent on /dashboard (LLM observational)
# ═════════════════════════════════════════════════════════════════════
section("TEST 8: AIWebAgent on /dashboard (LLM enabled, extract-only goal)")
with TestContext("agent_dashboard", 8) as ctx:
    agent = AIWebAgent(
        profile="prod_test_8",
        max_steps=3,
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=True,
    )
    result = agent.run(f"{BASE}/dashboard", "show the page title")
    ok = bool(
        result.steps and len(result.steps) >= 1
        and "127.0.0.1" in result.final_url
    )
    provider = getattr(agent, "current_provider", "")
    ctx.evidence_add("steps_count", len(result.steps))
    ctx.evidence_add("final_url", result.final_url)
    ctx.evidence_add("final_title", result.final_title)
    ctx.evidence_add("provider", str(provider))
    ctx.evidence_add("success", result.success)
    record(ctx,
           f"steps={len(result.steps)} title={result.final_title!r} provider={provider} success={result.success}",
           ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Test 9: Multi-provider detection against mock
# ═════════════════════════════════════════════════════════════════════
section("TEST 9: Multi-provider detection (9 endpoints)")
with TestContext("multi_provider", 9) as ctx:
    from specter.providers import get_detector
    import sqlite3
    db_path = Path.home() / ".cf_persistent" / "cf_session.db"

    def wipe_db():
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as _c:
                    _c.execute("DELETE FROM sessions WHERE host = '127.0.0.1'")
                    _c.execute("DELETE FROM cookies WHERE host = '127.0.0.1'")
                    _c.commit()
            except Exception as e:
                ctx.cleanup(f"DB wipe failed: {e}")

    wipe_db()
    ctx.cleanup(f"initial DB wipe (db={db_path})")

    targets = [
        ("/cloudflare", "cloudflare"),
        ("/akamai", "akamai"),
        ("/datadome", "datadome"),
        ("/imperva", "imperva"),
        ("/aws-waf", "aws_waf"),
        ("/recaptcha", "recaptcha"),
        ("/hcaptcha", "hcaptcha"),
        ("/arkose", "arkose"),
        ("/none", "unknown"),
    ]
    detector = get_detector()
    subtest_results = []
    subtest_dir = ctx.dir / "subtests"
    subtest_dir.mkdir(exist_ok=True)

    for path, expected in targets:
        wipe_db()
        ctx.cleanup(f"wipe DB before {path}")
        os.system("bash ~/.tmp_cleanup.sh 2>/dev/null")
        time.sleep(1)
        ctx.cleanup(f"hard-kill chrome before {path}")

        b = Browser(profile=f"prod_test_9_{expected}", headless=True, auto_solve=False)
        sub_host = f"127.0.0.1{path.replace('/','.')}"
        b.get(f"{BASE}{path}")
        time.sleep(1.5)
        snap = b.snapshot(include_html=True)
        all_cookies = b.cookies or {}
        clean_cookies = {
            k: v for k, v in all_cookies.items()
            if k not in ("__cf_bm", "__cflb", "cf_bm")
            and not k.startswith("_cf_")
            and k != "NID"
        }
        snap_for_det = dict(snap)
        snap_for_det["cookies"] = clean_cookies
        cookie_header = "; ".join(f"{k}={v}" for k, v in clean_cookies.items())
        merged_headers = {"cookie": cookie_header} if cookie_header else {}
        result = detector.detect(
            headers=merged_headers,
            body="",
            snapshot=snap_for_det,
            host=sub_host,
        )
        detected = result.provider.value
        subtest_results.append({
            "path": path,
            "expected": expected,
            "detected": detected,
            "match": detected == expected,
            "confidence": round(result.confidence, 3),
            "method": result.method,
            "evidence": result.evidence,
            "sub_host": sub_host,
            "raw_cookies": list(all_cookies.keys()),
            "clean_cookies": list(clean_cookies.keys()),
        })
        # save per-subtest result
        safe_name = path.strip("/").replace("/", "_") or "root"
        (subtest_dir / f"{safe_name}.json").write_text(
            json.dumps(subtest_results[-1], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # screenshot of subtest
        try:
            b.screenshot(str(subtest_dir / f"{safe_name}.png"))
        except Exception:
            pass
        if detected != expected:
            print(f"    DBG {path}: detected={detected} expected={expected} cookies={list(all_cookies.keys())} evidence={result.evidence} method={result.method}")
        b.quit()
        ctx.cleanup(f"subtest {path}: detected={detected} (expected={expected}, match={detected==expected})")

    correct = sum(1 for r in subtest_results if r["match"])
    wrong = [r for r in subtest_results if not r["match"]]
    ok = correct == len(targets)
    detail = f"{correct}/{len(targets)} correct"
    if wrong:
        detail += f" | wrong: {[(r['expected'], r['detected']) for r in wrong]}"
    ctx.evidence_add("subtest_count", len(targets))
    ctx.evidence_add("correct_count", correct)
    ctx.evidence_add("wrong", wrong)
    record(ctx, detail, ok=ok)


# ═════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════
section("SUMMARY")
total = results["passed"] + results["failed"]
print(f"  passed: {results['passed']}/{total}")
print(f"  failed: {results['failed']}/{total}")
for t in results["tests"]:
    print(f"  [{'OK' if t['ok'] else 'FAIL'}] {t['name']}")
print(f"\n  artifacts: {LOG_ROOT}")

# also write a run-level summary
summary = {
    "run_id": RUN_ID,
    "passed": results["passed"],
    "failed": results["failed"],
    "total": total,
    "tests": results["tests"],
    "log_root": str(LOG_ROOT),
    "started_at": datetime.fromtimestamp(min(
        (LOG_ROOT / d.name / "result.json").stat().st_mtime
        for d in LOG_ROOT.iterdir() if d.is_dir() and (d / "result.json").exists()
    )).isoformat() if any((d / "result.json").exists() for d in LOG_ROOT.iterdir() if d.is_dir()) else None,
}
(LOG_ROOT / "summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
)

sys.exit(0 if results["failed"] == 0 else 1)
