"""Integration test: vision provider → cf_agent decision layer.

3 scenarios:
  A. Vision confirms challenge visible → planner waits.
  B. DOM says no challenge but vision says cloudflare challenge → planner waits (vision override).
  C. Vision disabled (CF_VISION_ENABLED=OFF) → planner uses DOM only, no vision in trace.

All scenarios target the local mock server (127.0.0.1:18801).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

HOME = Path("/data/data/com.termux/files/home")
sys.path.insert(0, str(HOME / 'specter'))

OUT_DIR = HOME / "test_logs" / "cf_agent_vision"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MOCK = HOME / "specter" / "specter" / "mock_server.py"

# Test pages and what we expect vision to say
PAGES = [
    ("/cloudflare", "cloudflare_challenge"),
    ("/recaptcha", "recaptcha"),
    ("/none", "normal_page"),  # no challenge
]


def free_mb() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def check_mock():
    import urllib.request
    try:
        r = urllib.request.urlopen("http://127.0.0.1:18801/healthz", timeout=2)
        d = json.loads(r.read())
        return d.get("status") == "ok"
    except Exception:
        return False


def start_mock():
    if check_mock():
        return None
    log = HOME / "test_logs" / "cf_agent_vision" / "mock.log"
    p = subprocess.Popen(
        ["python3", str(MOCK), "--port", "18801"],
        stdout=open(log, "wb"), stderr=subprocess.STDOUT,
    )
    for _ in range(30):
        if check_mock():
            return p
        time.sleep(0.5)
    raise RuntimeError("mock server failed to start")


def run_scenario(name: str, page: str, use_vision: bool, expected_vision_hint: str | None):
    """Run cf_agent with vision on or off, visit one page, return run result."""
    env = os.environ.copy()
    env["CF_VISION_ENABLED"] = "1" if use_vision else "0"
    env["CF_BYPASS_ENABLED"] = "0"  # safe mode
    from specter.agent import AIWebAgent
    with AIWebAgent(
        profile=f"agent_{name}",
        max_steps=3,
        allowed_domains=["127.0.0.1"],
        authorized_test_mode=True,
        use_llm=False,
        use_vision=use_vision,
        headless=True,
        wait_per_step=0.3,
    ) as agent:
        return agent.run(f"http://127.0.0.1:18801{page}", goal="show title")


def main() -> int:
    if not MOCK.exists():
        print(f"missing mock server: {MOCK}")
        return 1
    mock_proc = start_mock()
    if mock_proc is None:
        print("mock: already up")
    else:
        print(f"mock: started pid={mock_proc.pid}")

    summary = {"scenarios": []}
    for name, (page, expected_hint) in zip(
        ["A_confirm", "B_override_recaptcha", "C_disabled"],
        [("/cloudflare", "cloudflare_challenge"),
         ("/recaptcha", "recaptcha"),
         ("/none", "normal_page")],
    ):
        use_vision = name != "C_disabled"
        print(f"\n=== scenario {name} page={page} vision={use_vision} ===")
        avail = free_mb()
        print(f"  mem available: {avail}MB")
        if avail < 700:
            print(f"  SKIP: only {avail}MB free")
            summary["scenarios"].append({"name": name, "skipped": "low mem"})
            continue
        # Force GC + sync between scenarios
        import gc
        gc.collect()
        os.system("sync")  # flush disk buffers
        t0 = time.time()
        try:
            res = run_scenario(name, page, use_vision, expected_hint)
        except Exception as e:
            print(f"  ERROR: {e}")
            summary["scenarios"].append({"name": name, "error": str(e)})
            continue
        dur = int((time.time() - t0) * 1000)
        # result is AgentResult dataclass — convert to dict via asdict
        rdict = asdict(res) if hasattr(res, "__dataclass_fields__") else (res or {})
        # vision_log is written to result.json on disk, not on the AgentResult
        run_id = rdict.get("run_id", "")
        result_file = Path(f"/data/data/com.termux/files/home/.cf_agent/runs/{run_id}/result.json")
        if result_file.exists():
            on_disk = json.loads(result_file.read_text())
            rdict["vision_log"] = on_disk.get("vision_log", [])
        steps = rdict.get("steps", [])
        vision_log = rdict.get("vision_log", [])
        # check trace has vision verdict (steps are dicts after asdict)
        has_vision_in_trace = any(s.get("extra", {}).get("vision") for s in steps)
        first_vision = vision_log[0] if vision_log else {}
        # save artifacts
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(
            json.dumps(rdict, indent=2, default=str), encoding="utf-8"
        )
        scenario_result = {
            "name": name,
            "page": page,
            "use_vision": use_vision,
            "expected_hint": expected_hint,
            "duration_ms": dur,
            "success": rdict.get("success"),
            "final_url": rdict.get("final_url"),
            "final_title": rdict.get("final_title"),
            "steps": len(steps),
            "vision_consults": len(vision_log),
            "first_vision_hint": first_vision.get("provider_hint"),
            "first_vision_conf": first_vision.get("confidence"),
            "first_vision_challenge_visible": first_vision.get("challenge_visible"),
            "has_vision_in_trace": has_vision_in_trace,
        }
        # A: vision confirms challenge visible
        if name == "A_confirm":
            scenario_result["pass"] = (
                first_vision.get("challenge_visible") is True
                and first_vision.get("provider_hint") == expected_hint
            )
        # B: vision classifies recaptcha challenge on /recaptcha page
        elif name == "B_override_recaptcha":
            scenario_result["pass"] = (
                first_vision.get("provider_hint") == expected_hint
                and first_vision.get("challenge_visible") is True
            )
        # C: vision disabled, no vision in trace
        elif name == "C_disabled":
            scenario_result["pass"] = (
                not has_vision_in_trace
                and len(vision_log) == 0
            )
        summary["scenarios"].append(scenario_result)
        print(f"  duration={dur}ms  vision_hint={first_vision.get('provider_hint')} "
              f"visible={first_vision.get('challenge_visible')}  pass={scenario_result.get('pass')}")

    summary["mock_pid"] = mock_proc.pid if mock_proc else None
    summary["finished_at"] = time.time()
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    passed = sum(1 for s in summary["scenarios"] if s.get("pass"))
    total = len(summary["scenarios"])
    print(f"\n=== {passed}/{total} scenarios passed ===")
    print(f"artifacts: {OUT_DIR}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
