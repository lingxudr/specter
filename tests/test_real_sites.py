"""Real-world test: cf_selenium.Browser against actual public websites.
No mock server. Tests that the agent can navigate real, unprotected sites.

Targets (chosen for low footprint + no heavy bot protection):
- example.com       : IANA reserved, minimal HTML
- example.org       : IANA reserved
- iana.org          : real site, no protection
- httpbin.org/get   : returns JSON, no protection
- info.cern.ch      : first website, ultra-minimal
"""

import json
import sys
import time
import random
from datetime import datetime
from pathlib import Path

# === setup paths ===
TEST_LOG_DIR = Path.home() / "test_logs" / f"run_real_{int(time.time())}_{random.randint(1000,9999):04d}"
TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)

# add home to path
sys.path.insert(0, str(Path.home() / 'specter'))

from cf_selenium import Browser


TARGETS = [
    {"name": "example_com",     "url": "https://example.com/",      "expect_title": "Example",      "expect_status": 200, "expect_body_marker": "Example Domain"},
    {"name": "example_org",     "url": "https://example.org/",      "expect_title": "Example",      "expect_status": 200, "expect_body_marker": "Example Domain"},
    {"name": "iana_root",       "url": "https://www.iana.org/",     "expect_title": "Internet Assigned Numbers", "expect_status": 200, "expect_body_marker": "Internet Assigned Numbers"},
    {"name": "httpbin_get",     "url": "https://httpbin.org/get",   "expect_title": "",             "expect_status": 200, "expect_body_marker": "\"url\""},
    {"name": "cern_first_web",  "url": "http://info.cern.ch/hypertext/WWW/TheProject.html", "expect_title": "The World Wide Web project", "expect_status": 200, "expect_body_marker": "World Wide Web"},
    {"name": "iana_about",      "url": "https://www.iana.org/about","expect_title": "About",       "expect_status": 200, "expect_body_marker": "About IANA"},
]


def run_one(target, n, total):
    name = target["name"]
    url = target["url"]
    sub = TEST_LOG_DIR / f"{n:02d}_{name}"
    sub.mkdir(exist_ok=True)
    log_lines = []
    evidence = {}
    ok = False
    detail = ""
    err = None
    started = datetime.now().isoformat()
    t0 = time.time()

    def log(msg):
        line = f"{datetime.now().isoformat()} {msg}"
        log_lines.append(line)
        print(line, flush=True)

    log(f"=== {name} ===")
    log(f"target: {url}")

    b = Browser(profile=f"real_test_{name}_{random.randint(1000,9999)}")
    try:
        log(f"navigating...")
        b.get(url, wait_for="body")
        # give it a sec for any JS to settle
        time.sleep(1.5)
        title = b.title
        snap = b.snapshot(include_html=True)
        cookies = b.cookies  # dict[name, value] (or list[dict] in some cf_selenium versions)
        if isinstance(cookies, dict):
            cookies_count = len(cookies)
            cookie_names = list(cookies.keys())
        elif isinstance(cookies, list):
            cookies_count = len(cookies)
            cookie_names = [c.get("name") if isinstance(c, dict) else str(c) for c in cookies]
        else:
            cookies_count = 0
            cookie_names = []
        evidence = {
            "url": snap.get("url") or "",
            "title": title,
            "html_len": len(snap.get("html") or ""),
            "cookies_count": cookies_count,
            "cookie_names": cookie_names,
        }
        log(f"title: {title!r}")
        log(f"url:   {evidence['url']}")
        log(f"html:  {evidence['html_len']} bytes, cookies: {cookies_count} {cookie_names}")

        # save screenshot
        png = sub / f"{name}.png"
        try:
            b.screenshot(str(png))
            log(f"screenshot: {png.name} ({png.stat().st_size}B)")
            evidence["screenshot"] = str(png)
        except Exception as e:
            log(f"screenshot failed: {e}")
            evidence["screenshot"] = None

        # detection
        log("running detector...")
        try:
            from specter.providers import get_detector
            det = get_detector()
            # get response headers via performance
            try:
                perf = b._run(b._cdp.send("Performance.getMetrics")) if b._cdp else {}
            except Exception:
                perf = {}
            r = det.detect(headers={}, body=snap.get("html", ""), snapshot=evidence, host=url)
            evidence["detection"] = {
                "provider": r.provider.value if hasattr(r.provider, "value") else str(r.provider),
                "confidence": r.confidence,
                "challenge": r.challenge_state.value if hasattr(r.challenge_state, "value") else str(r.challenge_state),
                "method": r.method,
                "evidence": r.evidence,
            }
            log(f"detection: {evidence['detection']['provider']} conf={r.confidence:.2f} ({r.method})")
        except Exception as e:
            log(f"detection failed: {e}")
            evidence["detection"] = {"error": str(e)}

        # validate
        title_ok = target["expect_title"].lower() in title.lower() if target["expect_title"] else True
        body = snap.get("html") or ""
        body_ok = target["expect_body_marker"] in body
        # status: best effort — check html for obvious error pages
        is_error_page = "404" in title or "not found" in title.lower() or "error" in title.lower()
        status_ok = not is_error_page

        evidence["checks"] = {
            "title_ok": title_ok,
            "body_ok": body_ok,
            "status_ok": status_ok,
        }

        ok = title_ok and body_ok and status_ok
        detail = f"title={title!r} body_marker={'✓' if body_ok else '✗'} cookies={cookies_count}"

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log(f"ERROR: {err}")
        ok = False
        detail = err
    finally:
        try:
            b.quit()
        except Exception as e:
            log(f"quit failed: {e}")

    duration = time.time() - t0
    ended = datetime.now().isoformat()

    result = {
        "test": name,
        "url": url,
        "ok": ok,
        "detail": detail,
        "duration_seconds": round(duration, 2),
        "started_at": started,
        "ended_at": ended,
        "evidence": evidence,
        "error": err,
    }
    if evidence.get("screenshot"):
        result["screenshot"] = evidence["screenshot"]

    (sub / "result.json").write_text(json.dumps(result, indent=2, default=str))
    (sub / "log.txt").write_text("\n".join(log_lines))

    return result, log_lines


def main():
    print(f"=== real-world test: {len(TARGETS)} sites ===")
    print(f"artifacts: {TEST_LOG_DIR}")
    print(f"started: {datetime.now().isoformat()}\n")

    summary = {
        "run_dir": str(TEST_LOG_DIR),
        "started_at": datetime.now().isoformat(),
        "total": len(TARGETS),
        "results": [],
    }
    passed = 0
    for i, t in enumerate(TARGETS, 1):
        r, _ = run_one(t, i, len(TARGETS))
        summary["results"].append({
            "test": r["test"],
            "ok": r["ok"],
            "duration": r["duration_seconds"],
            "detail": r["detail"],
        })
        if r["ok"]:
            passed += 1
        print()
        # check memory
        if i < len(TARGETS):
            try:
                import subprocess
                out = subprocess.check_output(["free", "-m"], text=True)
                for line in out.splitlines():
                    if line.startswith("Mem:"):
                        avail = int(line.split()[6])
                        print(f"  [mem] available: {avail}MB\n")
                        if avail < 400:
                            print(f"  [mem] LOW (<400MB) — Chrome will be tight")
                        break
            except Exception:
                pass

    summary["passed"] = passed
    summary["failed"] = len(TARGETS) - passed
    summary["ended_at"] = datetime.now().isoformat()
    (TEST_LOG_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(f"\n=== summary ===")
    print(f"passed: {passed}/{len(TARGETS)}, failed: {len(TARGETS)-passed}")
    print(f"artifacts: {TEST_LOG_DIR}")
    return 0 if passed == len(TARGETS) else 1


if __name__ == "__main__":
    sys.exit(main())
