"""test_e2e_integration.py — End-to-end integration test.

Combines: vision + multi-provider detection + AWS WAF token + session persistence.

Sub-tests (each in its own subprocess for memory isolation on 6GB HP):
  A. AWS WAF + valid token  → detection, vision, token applied, navigate
  B. AWS WAF + no token     → detection, vision, HumanRequiredError
  C. reCAPTCHA + vision     → detection, vision, planner escalation
  D. Token rotation         → store v1 → invalidate → store v2 → reload

All sub-tests run in fresh subprocesses via run_subtest() so Chrome memory
is freed between scenarios.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict
from pathlib import Path

HOME = Path("/data/data/com.termux/files/home")
sys.path.insert(0, str(HOME / 'specter'))

OUT_DIR = HOME / "test_logs" / "e2e_integration"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MOCK = HOME / "specter" / "specter" / "mock_server.py"
MOCK_PORT = 18801


# ── helpers ───────────────────────────────────────────────────────────
def free_mb() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def check_mock() -> bool:
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{MOCK_PORT}/healthz", timeout=2)
        return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def start_mock() -> subprocess.Popen | None:
    if check_mock():
        return None
    log = OUT_DIR / "mock.log"
    p = subprocess.Popen(
        ["python3", str(MOCK), "--port", str(MOCK_PORT)],
        stdout=open(log, "wb"), stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        if check_mock():
            return p
        time.sleep(0.5)
    raise RuntimeError("mock failed to start")


def run_subtest(label: str, fn_source: str, env_overrides: dict | None = None) -> dict:
    """Run a sub-test in a fresh subprocess. Returns parsed result dict."""
    work_dir = OUT_DIR / label
    work_dir.mkdir(parents=True, exist_ok=True)
    worker_path = work_dir / "_worker.py"
    worker_path.write_text(fn_source, encoding="utf-8")
    # Spawn with minimal env (don't pollute global)
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    # Force CF_BYPASS off; let sub-test opt in via env
    env.setdefault("CF_BYPASS_ENABLED", "0")
    log_path = work_dir / "log.txt"
    t0 = time.time()
    avail_before = free_mb()
    try:
        with open(log_path, "wb") as logf:
            proc = subprocess.run(
                [sys.executable, str(worker_path)],
                cwd=str(work_dir),
                env=env,
                stdout=logf, stderr=subprocess.STDOUT,
                timeout=180,
            )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    duration_ms = int((time.time() - t0) * 1000)
    avail_after = free_mb()
    # Worker writes its result to result.json in the work dir
    result_file = work_dir / "result.json"
    if not result_file.exists():
        return {
            "label": label, "pass": False, "duration_ms": duration_ms,
            "error": f"no result.json written (rc={rc})",
            "avail_before_mb": avail_before, "avail_after_mb": avail_after,
        }
    try:
        result = json.loads(result_file.read_text())
    except Exception as e:
        return {
            "label": label, "pass": False, "duration_ms": duration_ms,
            "error": f"result.json parse failed: {e}",
            "avail_before_mb": avail_before, "avail_after_mb": avail_after,
        }
    result["avail_before_mb"] = avail_before
    result["avail_after_mb"] = avail_after
    result["return_code"] = rc
    # re-save
    result_file.write_text(json.dumps(result, indent=2, default=str))
    return result


# ── sub-test worker source (subprocess) ───────────────────────────────
WORKER_HEADER = '''"""Worker for sub-test {LABEL}. Runs in fresh subprocess."""
import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path.home() / "specter"))

OUT = Path({OUT!r})
OUT.mkdir(parents=True, exist_ok=True)

# Force BypassSession to use a per-test DB so tests don't pollute each other
os.environ.setdefault("CF_AGENT_STATE_DIR", str(OUT / "state"))

result = {{"label": {LABEL!r}, "started_at": time.time()}}
try:
'''

WORKER_FOOTER = '''
except Exception as e:
    result["pass"] = False
    result["error"] = f"{e}\\n{traceback.format_exc()}"
finally:
    result["finished_at"] = time.time()
    (OUT / "result.json").write_text(json.dumps(result, indent=2, default=str))
'''


# ── A. AWS WAF + valid token ─────────────────────────────────────────
WORKER_A = WORKER_HEADER.format(LABEL="A_aws_waf_with_token", OUT=str(OUT_DIR / "A_aws_waf_with_token")) + '''
    from specter.agent import AIWebAgent
    from specter.providers import AWSWAFAdapter, get_token_store
    from specter.sessions import BypassSession
    from specter.providers.aws_waf_token import AWSWAFTokenStore

    # Use isolated token store + session DB for this test
    token_store = AWSWAFTokenStore(OUT / "tokens.json")
    bs = BypassSession(db_path=OUT / "sessions.db")

    host = "127.0.0.1"  # mock server
    # Pre-store a valid token (simulates out-of-band acquisition)
    adapter = AWSWAFAdapter(store=token_store)
    adapter.store_token(
        value="legit_token_for_e2e_A",
        host=host, max_age=3600, source="manual",
        notes="end-to-end integration test A",
    )
    # Also push to session layer
    bs.apply_aws_waf_token(
        host=host, token_value="legit_token_for_e2e_A",
        expires_in=3600, source="manual", notes="via session layer",
    )

    result["pre_token_state"] = adapter.token_state(host)
    result["pre_session_has_token"] = bs.has_aws_waf_token(host)

    # Run agent with vision ON
    agent = AIWebAgent(
        profile="e2e_A",
        max_steps=2,
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,
        use_vision=True,
        headless=True,
        wait_per_step=0.3,
    )
    try:
        ar = agent.run(f"http://{host}:18801/aws-waf", goal="show title")
    finally:
        agent.tracer.close()
        try:
            if agent.b:
                agent.b.quit()
        except Exception:
            pass

    rd = asdict(ar)
    rd["steps"] = [asdict(s) for s in rd.get("steps", []) if hasattr(s, "__dataclass_fields__")] if rd.get("steps") else []
    # vision_log attached after the fact
    rd["vision_log"] = agent.vision_log
    rd["current_provider"] = agent.current_provider
    rd["current_detection"] = agent.current_detection
    result["agent_result"] = {
        "success": rd.get("success"),
        "final_url": rd.get("final_url"),
        "final_title": rd.get("final_title"),
        "steps": len(rd.get("steps", [])),
        "vision_consults": len(rd.get("vision_log", [])),
        "first_vision_hint": rd.get("vision_log", [{}])[0].get("provider_hint") if rd.get("vision_log") else None,
        "current_provider": rd.get("current_provider"),
    }
    # Check session persistence
    result["post_token_state"] = adapter.token_state(host)
    result["post_session_has_token"] = bs.has_aws_waf_token(host)
    result["post_session_token"] = bs.get_aws_waf_token(host)

    # ── assertions ──
    checks = {
        "token_stored_pre": result["pre_token_state"]["has_usable_token"] is True,
        "session_has_token_pre": result["post_session_has_token"] is False,  # (just stored, but session may not have it before run)
        "provider_detected": rd.get("current_provider") == "aws_waf",
        "vision_saw_challenge": any(
            v.get("challenge_visible") for v in rd.get("vision_log", [])
        ),
        "vision_hint_correct": any(
            v.get("provider_hint") in ("aws_waf", "cloudflare_challenge")
            for v in rd.get("vision_log", [])
        ),
        "post_session_has_token": result["post_session_has_token"] is True,
    }
    # session_has_token_pre is informational; we set it before check
    result["session_has_token_pre"] = bs.has_aws_waf_token(host)
    checks["session_has_token_pre"] = result["session_has_token_pre"] is True
    result["checks"] = checks
    result["pass"] = all(checks.values())
''' + WORKER_FOOTER


# ── B. AWS WAF + no token ────────────────────────────────────────────
WORKER_B = WORKER_HEADER.format(LABEL="B_aws_waf_no_token", OUT=str(OUT_DIR / "B_aws_waf_no_token")) + '''
    from specter.agent import AIWebAgent
    from specter.providers import AWSWAFAdapter
    from specter.providers.aws_waf_token import AWSWAFTokenStore

    token_store = AWSWAFTokenStore(OUT / "tokens.json")
    adapter = AWSWAFAdapter(store=token_store)
    host = "127.0.0.1"

    # verify no token initially
    result["pre_token_state"] = adapter.token_state(host)
    assert result["pre_token_state"]["has_usable_token"] is False, "token store should be empty"

    # Direct adapter.solve() with no token should raise HumanRequiredError
    from specter.providers import HumanRequiredError
    try:
        adapter.solve(None, f"http://{host}:18801/aws-waf")
        result["solve_raised"] = False
        result["pass"] = False
        result["error"] = "solve() did not raise HumanRequiredError"
    except HumanRequiredError as e:
        result["solve_raised"] = True
        result["solve_error_msg"] = str(e)
        result["solve_hint"] = e.hint
        result["solve_provider"] = str(e.provider)

    # Also run the agent with vision ON, see if it stops gracefully
    agent = AIWebAgent(
        profile="e2e_B",
        max_steps=2,
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,
        use_vision=True,
        headless=True,
        wait_per_step=0.3,
    )
    try:
        ar = agent.run(f"http://{host}:18801/aws-waf", goal="show title")
    finally:
        agent.tracer.close()
        try:
            if agent.b:
                agent.b.quit()
        except Exception:
            pass

    rd = asdict(ar)
    rd["steps"] = [asdict(s) for s in rd.get("steps", []) if hasattr(s, "__dataclass_fields__")] if rd.get("steps") else []
    rd["current_provider"] = agent.current_provider
    rd["current_detection"] = agent.current_detection
    result["agent_result"] = {
        "success": rd.get("success"),
        "final_url": rd.get("final_url"),
        "final_title": rd.get("final_title"),
        "summary": rd.get("summary"),
        "steps": len(rd.get("steps", [])),
        "vision_consults": len(agent.vision_log),
        "first_vision_hint": agent.vision_log[0].get("provider_hint") if agent.vision_log else None,
        "human_required_error": str(agent.human_required_error) if agent.human_required_error else None,
    }
    checks = {
        "solve_raised_human_required": result.get("solve_raised") is True,
        "solve_hint_mentions_aws_waf": "aws-waf-token" in (result.get("solve_hint") or "").lower(),
        "agent_detected_provider": rd.get("current_provider") == "aws_waf",
        "vision_saw_challenge": any(
            v.get("challenge_visible") for v in agent.vision_log
        ),
    }
    result["checks"] = checks
    result["pass"] = all(checks.values())
''' + WORKER_FOOTER


# ── C. reCAPTCHA + vision ─────────────────────────────────────────────
WORKER_C = WORKER_HEADER.format(LABEL="C_recaptcha_vision", OUT=str(OUT_DIR / "C_recaptcha_vision")) + '''
    from specter.agent import AIWebAgent

    agent = AIWebAgent(
        profile="e2e_C",
        max_steps=2,
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,
        use_vision=True,
        headless=True,
        wait_per_step=0.3,
    )
    try:
        ar = agent.run("http://127.0.0.1:18801/recaptcha", goal="show title")
    finally:
        agent.tracer.close()
        try:
            if agent.b:
                agent.b.quit()
        except Exception:
            pass

    rd = asdict(ar)
    rd["steps"] = [asdict(s) for s in rd.get("steps", []) if hasattr(s, "__dataclass_fields__")] if rd.get("steps") else []
    rd["current_provider"] = agent.current_provider
    result["agent_result"] = {
        "success": rd.get("success"),
        "final_url": rd.get("final_url"),
        "final_title": rd.get("final_title"),
        "steps": len(rd.get("steps", [])),
        "vision_consults": len(agent.vision_log),
        "first_vision_hint": agent.vision_log[0].get("provider_hint") if agent.vision_log else None,
        "first_vision_challenge_visible": agent.vision_log[0].get("challenge_visible") if agent.vision_log else None,
        "current_provider": agent.current_provider,
        "human_required_error": str(agent.human_required_error) if agent.human_required_error else None,
    }
    checks = {
        "provider_detected": rd.get("current_provider") == "recaptcha",
        "vision_saw_challenge": any(
            v.get("challenge_visible") for v in agent.vision_log
        ),
        "vision_hint_recaptcha": any(
            v.get("provider_hint") == "recaptcha" for v in agent.vision_log
        ),
    }
    result["checks"] = checks
    result["pass"] = all(checks.values())
''' + WORKER_FOOTER


# ── D. Token rotation + session persistence ──────────────────────────
WORKER_D = WORKER_HEADER.format(LABEL="D_token_rotation", OUT=str(OUT_DIR / "D_token_rotation")) + '''
    from specter.providers import AWSWAFAdapter
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    from specter.sessions import BypassSession
    import shutil

    token_store = AWSWAFTokenStore(OUT / "tokens.json")
    bs = BypassSession(db_path=OUT / "sessions.db")
    adapter = AWSWAFAdapter(store=token_store)
    host = "127.0.0.1"

    # ── v1: store, persist to session ──
    adapter.store_token("v1_token", host, max_age=3600, source="browser")
    bs.apply_aws_waf_token(host, "v1_token", expires_in=3600, source="browser")
    s1 = adapter.token_state(host)
    result["after_v1"] = {
        "token_state": s1,
        "session_has": bs.has_aws_waf_token(host),
        "session_value": bs.get_aws_waf_token(host).get("value") if bs.get_aws_waf_token(host) else None,
    }

    # ── rotation: refresh v1, store v2 ──
    n_inv = adapter.refresh_token(host)  # reason="rotate"
    adapter.store_token("v2_token", host, max_age=3600, source="browser",
                        notes="rotated from v1 in test D")
    # Session layer is separate — also refresh
    bs.refresh_aws_waf_token(host)
    bs.apply_aws_waf_token(host, "v2_token", expires_in=3600, source="browser",
                            notes="session rotated from v1")
    s2 = adapter.token_state(host)
    result["after_v2"] = {
        "token_state": s2,
        "session_has": bs.has_aws_waf_token(host),
        "session_value": bs.get_aws_waf_token(host).get("value") if bs.get_aws_waf_token(host) else None,
        "refresh_inv_count": n_inv,
    }

    # ── simulate "restart" by re-instantiating from same files ──
    token_store_2 = AWSWAFTokenStore(OUT / "tokens.json")
    adapter_2 = AWSWAFAdapter(store=token_store_2)
    bs_2 = BypassSession(db_path=OUT / "sessions.db")
    s3 = adapter_2.token_state(host)
    result["after_restart"] = {
        "token_state": s3,
        "session_has": bs_2.has_aws_waf_token(host),
        "session_value": bs_2.get_aws_waf_token(host).get("value") if bs_2.get_aws_waf_token(host) else None,
    }

    # ── adapter.solve() after rotation should return v2 ──
    sol = adapter.solve(None, f"http://{host}:18801/aws-waf")
    result["solve_after_rotate"] = {
        "cookies": sol.get("cookies"),
        "expires_in": sol.get("expires_in"),
    }

    checks = {
        "v1_stored": result["after_v1"]["session_value"] == "v1_token",
        "v1_session_has": result["after_v1"]["session_has"] is True,
        "v2_stored": result["after_v2"]["session_value"] == "v2_token",
        "v2_session_has": result["after_v2"]["session_has"] is True,
        "v1_invalidated": result["after_v2"]["token_state"]["invalidated_count"] >= 1,
        "needs_refresh_before_v2_was": result["after_v1"]["token_state"]["needs_refresh"] is False,
        "restart_preserves_session": result["after_restart"]["session_value"] == "v2_token",
        "restart_session_has": result["after_restart"]["session_has"] is True,
        "solve_returns_v2": sol.get("cookies", {}).get("aws-waf-token") == "v2_token",
    }
    result["checks"] = checks
    result["pass"] = all(checks.values())
''' + WORKER_FOOTER


SUBTEST_DEFS = [
    ("A_aws_waf_with_token", WORKER_A, None),
    ("B_aws_waf_no_token", WORKER_B, None),
    ("C_recaptcha_vision", WORKER_C, None),
    ("D_token_rotation", WORKER_D, None),
]


def main() -> int:
    if not MOCK.exists():
        print(f"missing mock: {MOCK}")
        return 1
    p = start_mock()
    if p:
        print(f"mock started pid={p.pid}")
    else:
        print("mock already up")
    if not check_mock():
        print("mock not reachable, abort")
        return 1

    print(f"output dir: {OUT_DIR}")
    summary = {"subtests": [], "started_at": time.time()}
    for label, src, env_overrides in SUBTEST_DEFS:
        avail = free_mb()
        print(f"\n=== {label} (avail={avail}MB) ===")
        if avail < 600:
            print(f"  SKIP: only {avail}MB free")
            summary["subtests"].append({"label": label, "skipped": "low mem", "pass": False})
            continue
        t0 = time.time()
        result = run_subtest(label, src, env_overrides)
        result["total_duration_ms"] = int((time.time() - t0) * 1000)
        summary["subtests"].append(result)
        status = "✓ PASS" if result.get("pass") else "✗ FAIL"
        print(f"  {status} in {result['total_duration_ms']}ms "
              f"(avail before={result.get('avail_before_mb')}MB, after={result.get('avail_after_mb')}MB)")
        if not result.get("pass"):
            if result.get("error"):
                print(f"  error: {result['error'][:200]}")
            if "checks" in result:
                failed = [k for k, v in result["checks"].items() if not v]
                if failed:
                    print(f"  failed checks: {failed}")
        # Force GC + sleep to release memory
        import gc
        gc.collect()
        time.sleep(1)
    summary["finished_at"] = time.time()
    passed = sum(1 for s in summary["subtests"] if s.get("pass"))
    total = sum(1 for s in summary["subtests"] if not s.get("skipped"))
    skipped = sum(1 for s in summary["subtests"] if s.get("skipped"))
    summary["passed"] = passed
    summary["total_run"] = total
    summary["skipped"] = skipped
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n=== {passed}/{total} passed, {skipped} skipped ===")
    print(f"artifacts: {OUT_DIR}/summary.json")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
