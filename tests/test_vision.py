"""Test cf_agent_vision against mock challenge pages.

Each page runs in its own subprocess (Browser + tesseract), so memory is
fully reclaimed between pages — needed on the 6GB Android device where
Chrome ~400MB + tesseract leaks quickly.

Aggregates results into OUT_DIR/results.json and prints a final summary.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

MOCK = "http://127.0.0.1:18801"
# 4 pages — diverse challenge types + 1 normal
# Just 2 pages — memory-tight on 6GB
PAGES = [
    ("/cloudflare", "cloudflare_challenge"),
    ("/recaptcha", "recaptcha"),
    ("/hcaptcha", "hcaptcha"),
]

OUT_DIR = Path("/data/data/com.termux/files/home/test_logs/vision_test")
OUT_DIR.mkdir(parents=True, exist_ok=True)
PER_DIR = OUT_DIR / "subtests"
PER_DIR.mkdir(parents=True, exist_ok=True)

WORKER = r"""
import sys, json, time
from pathlib import Path
# Worker lives in test_logs/vision_test/_worker.py — add home (parent of test_vision.py) to path
sys.path.insert(0, "/data/data/com.termux/files/home")

from cf_selenium import Browser
from specter.vision import get_provider, VisionTask

path, expected, work_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
work_dir.mkdir(parents=True, exist_ok=True)

url = "http://127.0.0.1:18801" + path
b = Browser(profile="vision_" + path.strip("/"))
try:
    b.get(url, wait_for="body")
    time.sleep(0.5)
    png_path = b.screenshot(work_dir / (path.strip("/") + ".png"))
    snap = b.snapshot(include_html=True)
    # cf_selenium.snapshot() doesn't return raw accessibility tree — build it from visible e
    # Note: this template is only used by standalone test_vision_*.py; the integration
    # test (test_cf_agent_vision.py) uses cf_agent.AIWebAgent directly which feeds the
    # plain cf_selenium snapshot (no synthesized accessibility) to the vision module.
    ax = []
    for h in snap.get("headings", []):
        if isinstance(h, dict):
            ax.append({"role": "heading", "name": h.get("text", "")})
        else:
            ax.append({"role": "heading", "name": str(h)})
    for b_data in snap.get("buttons", []):
        if isinstance(b_data, dict):
            ax.append({"role": "button", "name": b_data.get("text", "")})
        else:
            ax.append({"role": "button", "name": str(b_data)})
    for l in snap.get("links", []):
        if isinstance(l, dict):
            ax.append({"role": "link", "name": l.get("text", "")})
        else:
            ax.append({"role": "link", "name": str(l)})
    if snap.get("text_preview"):
        ax.append({"role": "paragraph", "name": snap["text_preview"][:300]})
    dom_snap = {
        "url": snap.get("url", ""),
        "title": snap.get("title", ""),
        "accessibility": ax,
    }
    tess = get_provider("tesseract")
    tess_r = tess.analyze(str(png_path), task=VisionTask.OCR)
    dom = get_provider("dom")
    dom_r = dom.analyze_snapshot(dom_snap, task=VisionTask.CLASSIFY)
    out = {
        "page": path,
        "expected": expected,
        "tesseract_ok": tess_r.ok,
        "tesseract_err": tess_r.error,
        "tesseract_text": tess_r.text[:200],
        "dom_classification": dom_r.label,
        "dom_match": dom_r.label == expected,
        "dom_text_lines": len(dom_r.text.splitlines()),
    }
except Exception as e:
    out = {"page": path, "expected": expected, "error": str(e)}
finally:
    try:
        b.quit()
    except Exception:
        pass

# Write result file in work_dir so parent can pick it up
(work_dir / "result.json").write_text(json.dumps(out))
print(json.dumps(out))
"""


def list_provider_status():
    """Print provider status before subprocesses spin up Chrome."""
    from specter.vision import list_providers
    print("=== Vision providers ===")
    for p in list_providers():
        print(f"  {p['name']:14s} ready={p['ready']:5}  {p['reason']}")


def mem_available_mb() -> int:
    """Read MemAvailable from /proc/meminfo."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:
        return 0
    return 0


def kill_chrome():
    """Aggressively kill Chrome to free memory."""
    import subprocess
    subprocess.run(["pkill", "-9", "-f", "chrome"], capture_output=True)
    time.sleep(1)


def run_one(path: str, expected: str) -> dict:
    work = PER_DIR / path.strip("/")
    work.mkdir(parents=True, exist_ok=True)
    # Worker is written next to test_vision.py so it can import the same modules
    worker_path = OUT_DIR / "_worker.py"
    worker_path.write_text(WORKER)
    # Pre-kill any leftover Chrome to free memory
    kill_chrome()
    avail = mem_available_mb()
    print(f"  -> {path} (avail={avail}MB) ...", end=" ", flush=True)
    try:
        proc = subprocess.run(
            [sys.executable, str(worker_path), path, expected, str(work)],
            cwd=str(HERE),
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Worker prints JSON to stdout AND writes result.json
        if proc.returncode != 0:
            print(f"exit={proc.returncode}")
            print(f"      stderr: {proc.stderr[:300]}")
            return {"page": path, "expected": expected, "error": f"exit {proc.returncode}: {proc.stderr[:200]}"}
        result_file = work / "result.json"
        if result_file.exists():
            data = json.loads(result_file.read_text())
            ok = data.get("dom_match", False)
            tess_ok = data.get("tesseract_ok", False)
            print(
                f"dom={data.get('dom_classification', '?'):22s} "
                f"tess={'OK' if tess_ok else 'ERR':3s} "
                f"match={'✓' if ok else '✗'}"
            )
            return data
        print(f"no result.json — stdout: {proc.stdout[:200]}")
        return {"page": path, "expected": expected, "error": "no result.json"}
    except subprocess.TimeoutExpired:
        print("TIMEOUT")
        return {"page": path, "expected": expected, "error": "timeout 120s"}
    except Exception as e:
        print(f"ERROR: {e}")
        return {"page": path, "expected": expected, "error": str(e)}


def main():
    list_provider_status()
    print(f"\n=== Capturing {len(PAGES)} pages (one subprocess per page) ===")
    t0 = time.time()
    results = []
    for path, expected in PAGES:
        results.append(run_one(path, expected))
    elapsed = time.time() - t0
    matched = sum(1 for r in results if r.get("dom_match"))
    tess_ok = sum(1 for r in results if r.get("tesseract_ok"))
    total = len(results)
    print(f"\n=== Summary ({elapsed:.1f}s) ===")
    print(f"  DOM classification: {matched}/{total} correct")
    print(f"  Tesseract OCR:      {tess_ok}/{total} ok")
    (OUT_DIR / "results.json").write_text(json.dumps(results, indent=2))
    print(f"  Artifacts: {OUT_DIR}")


if __name__ == "__main__":
    main()
