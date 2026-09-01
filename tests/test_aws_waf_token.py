"""test_aws_waf_token.py — Integration test for AWS WAF token lifecycle API.

Scope: detection + token lifecycle only. NEVER auto-obtain.

Tests:
  A. Store + Load: store token, load returns it
  B. Solve with valid token: returns cookies dict, no exception
  C. Solve without token: raises HumanRequiredError
  D. Invalidate: store → invalidate → load returns None
  E. Detection (signature): /aws-waf response → provider=AWS_WAF
  F. BypassSession: apply_aws_waf_token + has_aws_waf_token + get_aws_waf_token
  G. Expiry: store with max_age=1, sleep 2s, load returns None
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HOME = Path("/data/data/com.termux/files/home")
sys.path.insert(0, str(HOME / 'specter'))

OUT_DIR = HOME / "test_logs" / "aws_waf_token"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MOCK = HOME / "specter" / "specter" / "mock_server.py"


def free_mb() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def check_mock():
    try:
        r = urllib.request.urlopen("http://127.0.0.1:18801/healthz", timeout=2)
        return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


def start_mock():
    if check_mock():
        return None
    log = OUT_DIR / "mock.log"
    p = subprocess.Popen(
        ["python3", str(MOCK), "--port", "18801"],
        stdout=open(log, "wb"), stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        if check_mock():
            return p
        time.sleep(0.5)
    raise RuntimeError("mock server failed to start")


def fetch_raw(path: str) -> tuple[int, dict, list, str]:
    """Issue a raw HTTP GET to mock, return (status, headers, set_cookies, body)."""
    req = urllib.request.Request(f"http://127.0.0.1:18801{path}")
    try:
        resp = urllib.request.urlopen(req, timeout=2)
        status = resp.status
        headers = {k: v for k, v in resp.headers.items()}
        cookies_raw = resp.headers.get_all("Set-Cookie") or []
        body = resp.read().decode("utf-8", errors="replace")
        return status, headers, cookies_raw, body
    except Exception as e:
        return 0, {}, [], f"ERROR: {e}"


# ── test cases ───────────────────────────────────────────────────────
def test_A_store_load(tmp_store: Path) -> dict:
    """A. Store + Load: store token, load returns it."""
    from specter.providers import AWSWAFAdapter
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    store = AWSWAFTokenStore(tmp_store / "tokens.json")
    adapter = AWSWAFAdapter(store=store)
    host = "test-aws-waf.example.com"
    tok = adapter.store_token(
        value="real_token_abc123",
        host=host, max_age=3600, source="manual",
        notes="operator-injected for staging",
    )
    loaded = adapter.load_token(host)
    state = adapter.token_state(host)
    return {
        "name": "A_store_load",
        "host": host,
        "stored": tok.to_dict(),
        "loaded_equals_stored": (
            loaded is not None and loaded.value == tok.value
            and loaded.host == tok.host
        ),
        "state_has_usable": state["has_usable_token"],
        "state_total": state["total_stored"],
        "pass": (
            loaded is not None
            and loaded.value == tok.value
            and state["has_usable_token"] is True
            and state["total_stored"] == 1
        ),
    }


def test_B_solve_with_token(tmp_store: Path) -> dict:
    """B. Solve with valid token: returns cookies dict, no exception."""
    from specter.providers import AWSWAFAdapter, HumanRequiredError
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    store = AWSWAFTokenStore(tmp_store / "tokens.json")
    adapter = AWSWAFAdapter(store=store)
    host = "test-aws-waf-b.example.com"
    adapter.store_token("solve_token_xyz", host, max_age=3600, source="browser")
    try:
        result = adapter.solve(None, f"https://{host}/some/path")
        ok = (
            result.get("cookies", {}).get("aws-waf-token") == "solve_token_xyz"
            and result.get("host") == host
            and result.get("expires_in", 0) > 0
        )
        return {
            "name": "B_solve_with_token",
            "host": host,
            "result_keys": sorted(result.keys()),
            "cookies": result.get("cookies"),
            "expires_in": result.get("expires_in"),
            "source": result.get("source"),
            "pass": ok,
            "error": None,
        }
    except HumanRequiredError as e:
        return {
            "name": "B_solve_with_token",
            "host": host,
            "pass": False,
            "error": f"unexpected HumanRequiredError: {e}",
        }


def test_C_solve_without_token(tmp_store: Path) -> dict:
    """C. Solve without token: raises HumanRequiredError."""
    from specter.providers import AWSWAFAdapter, HumanRequiredError
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    store = AWSWAFTokenStore(tmp_store / "tokens.json")
    adapter = AWSWAFAdapter(store=store)
    host = "test-aws-waf-c.example.com"
    try:
        adapter.solve(None, f"https://{host}/")
        return {
            "name": "C_solve_without_token",
            "host": host,
            "pass": False,
            "error": "expected HumanRequiredError, none raised",
        }
    except HumanRequiredError as e:
        # provider may be str or ProviderId enum depending on how adapter passed it
        prov_str = str(e.provider)
        return {
            "name": "C_solve_without_token",
            "host": host,
            "error_message": str(e),
            "hint_present": bool(e.hint),
            "provider": prov_str,
            "pass": (
                "no aws-waf-token" in e.hint.lower()
                and prov_str == "ProviderId.AWS_WAF"  # str(enum) returns "ProviderId.AWS_WAF"
            ),
        }


def test_D_invalidate(tmp_store: Path) -> dict:
    """D. Invalidate: store → invalidate → load returns None."""
    from specter.providers import AWSWAFAdapter
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    store = AWSWAFTokenStore(tmp_store / "tokens.json")
    adapter = AWSWAFAdapter(store=store)
    host = "test-aws-waf-d.example.com"
    tok = adapter.store_token("token_to_invalidate", host, max_age=3600, source="browser")
    n_inv = adapter.invalidate_token(host, reason="logout")
    after = adapter.load_token(host)
    state = adapter.token_state(host)
    return {
        "name": "D_invalidate",
        "host": host,
        "invalidate_count": n_inv,
        "after_load": after is None,
        "state_total": state["total_stored"],
        "state_invalidated": state["invalidated_count"],
        "state_needs_refresh": state["needs_refresh"],
        "pass": (
            n_inv == 1
            and after is None
            and state["invalidated_count"] == 1
            and state["needs_refresh"] is True
        ),
    }


def test_E_detection_signature() -> dict:
    """E. Detection (signature): /aws-waf response → provider=AWS_WAF."""
    from specter.providers import AWSWAFAdapter, ProviderId, ChallengeState
    adapter = AWSWAFAdapter()
    if not check_mock():
        return {"name": "E_detection_signature", "skipped": "mock down"}
    status, headers, cookies_raw, body = fetch_raw("/aws-waf")
    # Build headers dict in expected format
    norm_headers = {}
    for k, v in headers.items():
        norm_headers[k.lower()] = v
    # include cookies in header-like blob
    if cookies_raw:
        norm_headers["set-cookie"] = "; ".join(cookies_raw)
    det = adapter.signature_detect(norm_headers, body, host="127.0.0.1")
    return {
        "name": "E_detection_signature",
        "status": status,
        "provider": det.provider,
        "confidence": det.confidence,
        "challenge_state": det.challenge_state,
        "evidence": det.evidence,
        "pass": (
            det.provider == ProviderId.AWS_WAF
            and det.confidence >= 0.6
            and det.challenge_state == ChallengeState.JS_CHALLENGE
        ),
    }


def test_F_bypass_session() -> dict:
    """F. BypassSession: apply_aws_waf_token + has_aws_waf_token + get_aws_waf_token."""
    from specter.sessions import BypassSession, ProviderId
    # Use a fresh DB to avoid interference
    db_path = Path(tempfile.mkdtemp()) / "sessions.db"
    bs = BypassSession(db_path=db_path)
    host = "test-aws-waf-f.example.com"
    rec = bs.apply_aws_waf_token(
        host=host, token_value="session_token_f",
        expires_in=3600, source="manual", notes="integration test",
    )
    has = bs.has_aws_waf_token(host)
    got = bs.get_aws_waf_token(host)
    # Try refresh (invalidate)
    refreshed = bs.refresh_aws_waf_token(host)
    has_after = bs.has_aws_waf_token(host)
    return {
        "name": "F_bypass_session",
        "host": host,
        "has_after_apply": has,
        "token_value": got["value"] if got else None,
        "expires_in": got["expires_in"] if got else None,
        "refreshed": refreshed,
        "has_after_refresh": has_after,
        "pass": (
            has is True
            and got is not None
            and got["value"] == "session_token_f"
            and got["expires_in"] > 0
            and refreshed is True
            and has_after is False
        ),
    }


def test_G_expiry(tmp_store: Path) -> dict:
    """G. Expiry: store with max_age=1, sleep 2s, load returns None."""
    from specter.providers import AWSWAFAdapter
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    store = AWSWAFTokenStore(tmp_store / "tokens.json")
    adapter = AWSWAFAdapter(store=store)
    host = "test-aws-waf-g.example.com"
    adapter.store_token("short_lived_token", host, max_age=1, source="staging")
    immediately = adapter.load_token(host)
    time.sleep(2)
    after_expiry = adapter.load_token(host)
    state = adapter.token_state(host)
    return {
        "name": "G_expiry",
        "host": host,
        "loaded_immediately": immediately is not None,
        "loaded_after_2s": after_expiry is None,
        "state_expired_count": state["expired_count"],
        "state_needs_refresh": state["needs_refresh"],
        "pass": (
            immediately is not None
            and after_expiry is None
            and state["expired_count"] >= 1
            and state["needs_refresh"] is True
        ),
    }


def test_H_mintlifecycle_rotation(tmp_store: Path) -> dict:
    """H. Rotation: store old → store new (same value refreshed) → invalidate old by reason.

    This exercises the legitimate flow: token rotated, old marked 'rotate' reason.
    """
    from specter.providers import AWSWAFAdapter
    from specter.providers.aws_waf_token import AWSWAFTokenStore
    store = AWSWAFTokenStore(tmp_store / "tokens.json")
    adapter = AWSWAFAdapter(store=store)
    host = "test-aws-waf-h.example.com"
    # store v1
    adapter.store_token("v1", host, max_age=3600, source="browser")
    # rotate: invalidate v1
    adapter.refresh_token(host)
    # store v2 (fresh, after rotation)
    adapter.store_token("v2", host, max_age=3600, source="browser",
                        notes="rotated from v1")
    # now load should return v2
    usable = adapter.load_token(host)
    # purge_expired should NOT clean v1 (invalidated but not expired — only expired+invalidated are purged)
    purged = adapter.purge_expired(host)
    return {
        "name": "H_rotation",
        "host": host,
        "usable_value": usable.value if usable else None,
        "purged_count": purged,
        "pass": (
            usable is not None
            and usable.value == "v2"
            and usable.notes == "rotated from v1"
            and purged == 0  # v1 invalidated but not expired → not purged
        ),
    }


# ── main ─────────────────────────────────────────────────────────────
def main() -> int:
    if not MOCK.exists():
        print(f"missing mock server: {MOCK}")
        return 1
    mock_proc = start_mock()
    if mock_proc:
        print(f"mock: started pid={mock_proc.pid}")
    else:
        print("mock: already up")

    tmp_store = Path(tempfile.mkdtemp(prefix="aws_waf_test_"))
    print(f"tmp store dir: {tmp_store}")

    tests = [
        test_A_store_load,
        test_B_solve_with_token,
        test_C_solve_without_token,
        test_D_invalidate,
        test_E_detection_signature,
        test_F_bypass_session,
        test_G_expiry,
        test_H_mintlifecycle_rotation,
    ]

    summary = {"tests": [], "finished_at": time.time()}
    for t in tests:
        name = t.__name__
        print(f"\n=== {name} ===")
        t0 = time.time()
        try:
            # tests that need a tmp store take it as an arg; others take none
            result = t(tmp_store) if "tmp_store" in (t.__code__.co_varnames[: t.__code__.co_argcount]) else t()
        except Exception as e:
            import traceback
            result = {"name": name, "pass": False, "error": f"{e}\n{traceback.format_exc()}"}
        result["duration_ms"] = int((time.time() - t0) * 1000)
        summary["tests"].append(result)
        status = "PASS" if result.get("pass") else "FAIL"
        print(f"  → {status} ({result['duration_ms']}ms)")
        if not result.get("pass") and result.get("error"):
            print(f"  error: {result['error']}")
        elif not result.get("pass"):
            print(f"  detail: {json.dumps(result, indent=2, default=str)[:800]}")

    passed = sum(1 for t in summary["tests"] if t.get("pass"))
    total = len(summary["tests"])
    summary["passed"] = passed
    summary["total"] = total
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n=== {passed}/{total} tests passed ===")
    print(f"artifacts: {OUT_DIR}/summary.json")

    # cleanup tmp
    try:
        shutil.rmtree(tmp_store)
    except Exception:
        pass

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
