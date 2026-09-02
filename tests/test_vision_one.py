"""Lightweight vision test: one page at a time."""
import sys
import os
import gc
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/data/data/com.termux/files/home")

from cf_agent_vision import list_providers, get_provider, VisionTask

MOCK = "http://127.0.0.1:18801"
OUT_DIR = Path("/data/data/com.termux/files/home/test_logs/vision_one")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PAGE = sys.argv[1] if len(sys.argv) > 1 else "/cloudflare"
EXPECTED = sys.argv[2] if len(sys.argv) > 2 else "cloudflare_challenge"

# Free memory hint
import ctypes
try:
    libc = ctypes.CDLL("libc.so.6")
    libc.malloc_trim(0)
except Exception:
    pass

from cf_selenium import Browser

url = MOCK + PAGE
profile = f"vis_{PAGE.strip('/')}_{int(time.time())}"
print(f"=== {PAGE} (expect {EXPECTED}) ===")
print(f"profile: {profile}")

try:
    b = Browser(profile=profile)
    b.get(url, wait_for="body")
    time.sleep(0.6)

    png_path = OUT_DIR / f"{PAGE.strip('/')}.png"
    out = b.screenshot(png_path)
    print(f"screenshot: {out} ({out.stat().st_size if out.exists() else 0}B)")

    snap = b.snapshot(include_html=True)
    dom_snap = {
        "url": snap.get("url", ""),
        "title": snap.get("title", ""),
        "accessibility": snap.get("accessibility", []),
    }

    # Vision providers
    tess = get_provider("tesseract")
    tess_r = tess.analyze(str(out), task=VisionTask.OCR)
    print(f"tesseract: ok={tess_r.ok} text={tess_r.text[:80]!r}")
    if not tess_r.ok:
        print(f"  err: {tess_r.error}")

    dom = get_provider("dom")
    dom_class = dom.analyze_snapshot(dom_snap, task=VisionTask.CLASSIFY)
    dom_text = dom.analyze_snapshot(dom_snap, task=VisionTask.OCR)
    print(f"dom_class: {dom_class.label!r} (match={dom_class.label == EXPECTED})")
    print(f"dom_text: {dom_text.text[:80]!r}")

    result = {
        "page": PAGE,
        "expected": EXPECTED,
        "title": snap.get("title", ""),
        "tesseract_ok": tess_r.ok,
        "tesseract_text": tess_r.text[:200],
        "tesseract_err": tess_r.error,
        "dom_class": dom_class.label,
        "dom_match": dom_class.label == EXPECTED,
        "dom_text_first": dom_text.text.split("\n")[0] if dom_text.text else "",
    }
    (OUT_DIR / f"{PAGE.strip('/')}.json").write_text(json.dumps(result, indent=2))
    print("OK")
except Exception as e:
    import traceback
    print(f"ERROR: {e}")
    traceback.print_exc()
finally:
    try:
        b.quit()
    except Exception:
        pass
    gc.collect()
    try:
        libc.malloc_trim(0)
    except Exception:
        pass
