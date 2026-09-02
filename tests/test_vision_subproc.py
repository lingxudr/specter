"""Vision test: Termux Python, tesseract via proot subprocess."""
import sys
import os
import json
import time
import shutil
import subprocess
from pathlib import Path

sys.path.insert(0, "/data/data/com.termux/files/home")
from cf_agent_vision import get_provider, VisionTask
from cf_selenium import Browser

MOCK = "http://127.0.0.1:18801"
OUT_DIR = Path("/data/data/com.termux/files/home/test_logs/vision_subproc")
OUT_DIR.mkdir(parents=True, exist_ok=True)

PAGE = sys.argv[1] if len(sys.argv) > 1 else "/cloudflare"
EXPECTED = sys.argv[2] if len(sys.argv) > 2 else "cloudflare_challenge"

# Check tesseract via proot
def tesseract_ocr(png_path: str) -> str:
    """Run tesseract in proot Ubuntu on a PNG file."""
    # Path inside proot: /data/data/com.termux/files/home/... 
    rel = os.path.relpath(png_path, "/data/data/com.termux/files/home")
    inside = f"/data/data/com.termux/files/home/{rel}"
    try:
        r = subprocess.run(
            ["proot-distro", "login", "ubuntu", "--", "tesseract", inside, "-", "-l", "eng"],
            capture_output=True, text=True, timeout=20
        )
        if r.returncode == 0:
            return r.stdout.strip()
        return f"ERR:{r.stderr.strip()[:100]}"
    except subprocess.TimeoutExpired:
        return "ERR:timeout"
    except FileNotFoundError:
        return "ERR:proot-not-found"

url = MOCK + PAGE
profile = f"vsub_{PAGE.strip('/')}_{int(time.time())}"
print(f"=== {PAGE} (expect {EXPECTED}) ===", flush=True)

t0 = time.time()
b = Browser(profile=profile)
b.get(url, wait_for="body")
time.sleep(0.6)

png_path = OUT_DIR / f"{PAGE.strip('/')}.png"
out = b.screenshot(png_path)
size = out.stat().st_size if out.exists() else 0
print(f"screenshot: {out.name} ({size}B) [{time.time()-t0:.1f}s]", flush=True)

snap = b.snapshot(include_html=True)
title = snap.get("title", "")
url_got = snap.get("url", "")
print(f"title: {title!r} url: {url_got}", flush=True)

# Tesseract via proot
print("OCR via tesseract (proot)...", flush=True)
t1 = time.time()
ocr_text = tesseract_ocr(str(out))
print(f"tesseract: {time.time()-t1:.1f}s text={ocr_text[:120]!r}", flush=True)

# DOM provider
dom = get_provider("dom")
dom_snap = {"url": url_got, "title": title, "accessibility": snap.get("accessibility", [])}
dom_class = dom.analyze_snapshot(dom_snap, task=VisionTask.CLASSIFY)
print(f"dom_class: {dom_class.label!r} (expected={EXPECTED}, match={dom_class.label == EXPECTED})", flush=True)

result = {
    "page": PAGE,
    "expected": EXPECTED,
    "title": title,
    "tesseract_text": ocr_text,
    "dom_class": dom_class.label,
    "dom_match": dom_class.label == EXPECTED,
}
(OUT_DIR / f"{PAGE.strip('/')}.json").write_text(json.dumps(result, indent=2))

b.quit()
print("DONE", flush=True)
