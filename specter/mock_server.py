#!/usr/bin/env python3
"""
cf_agent_mock_server.py — stdlib HTTP server for staging/integration tests.

Endpoints (bind 127.0.0.1 only):
  GET /cloudflare     → CF headers + cf_clearance cookie + cf-challenge body
  GET /akamai         → Akamai headers + ak_bmsc cookie + sensor_data body
  GET /datadome       → DataDome cookie + captcha iframe
  GET /imperva        → Imperva cookie + incap_ses_/reese84 markers
  GET /aws-waf        → AWS WAF token + challenge form
  GET /aws-waf/refresh → issue a NEW aws-waf-token (simulates legitimate refresh)
  GET /aws-waf/invalidate → tell agent the current token is invalid (server-side)
  GET /aws-waf/expired → return a token that's already expired (Max-Age=1)
  GET /recaptcha      → reCAPTCHA script
  GET /hcaptcha       → hCaptcha script
  GET /arkose         → Arkose / FunCaptcha script
  GET /none           → clean page, no provider
  GET /rotate         → reset session state, force re-detect
  GET /healthz        → 200 ok

Usage:
  python3 cf_agent_mock_server.py [--port 18801]
  then: curl http://127.0.0.1:18801/cloudflare
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


# ── Token counter (simulates AWS WAF rotating tokens) ────────────────
_AWS_WAF_TOKEN_COUNTER = {"count": 0, "invalidated_tokens": set(), "issued": {}}


# ── Per-provider response templates ──────────────────────────────────
def _resp_cloudflare() -> tuple[int, dict, list, str]:
    headers = {
        "server": "cloudflare",
        "cf-ray": "12345abc-SJC",
        "cf-cache-status": "DYNAMIC",
        "content-type": "text/html; charset=utf-8",
    }
    cookies = [
        "cf_clearance=test_clearance_abc; Max-Age=3600; Path=/; Secure",
        "__cf_bm=test_bm_xyz; Max-Age=1800; Path=/; HttpOnly; Secure",
    ]
    body = """<html><head><title>Mock CF Page</title></head>
<body>
<h1>Cloudflare Mock</h1>
<p>Challenge page (mock): cf-challenge-running</p>
<div class="cf-turnstile" data-sitekey="0x4AAAAAAA"></div>
<p>cf-spinner</p>
</body></html>"""
    return 200, headers, cookies, body


def _resp_akamai() -> tuple[int, dict, list, str]:
    headers = {
        "server": "AkamaiGHost",
        "x-akamai-transformed": "9 63283 0 pmb=mto,824",
        "x-acg-cache-status": "1",
        "x-akamai-request-id": "1.2.3.4",
        "content-type": "text/html; charset=utf-8",
    }
    cookies = [
        "ak_bmsc=test_ak_bmsc; Max-Age=3600; Path=/",
        "bm_sz=test_bm_sz; Max-Age=3600; Path=/",
    ]
    body = """<html><head><title>Mock Akamai</title></head>
<body>
<h1>Akamai Mock</h1>
<script src="akam-test-cookie.js"></script>
<p>sensor_data: {challenge: "test", token: "fake"}</p>
<script>_bmr.js loader</script>
</body></html>"""
    return 200, headers, cookies, body


def _resp_datadome() -> tuple[int, dict, list, str]:
    headers = {
        "x-datadome-clientid": "test_client_id",
        "x-datadome-campaignid": "test_campaign",
        "content-type": "text/html; charset=utf-8",
    }
    cookies = ["datadome=test_dd_cookie; Max-Age=3600; Path=/"]
    body = """<html><head><title>Mock DataDome</title></head>
<body>
<h1>DataDome Mock</h1>
<script src="datadome.js"></script>
<iframe src="https://datadome.com/captcha/"></iframe>
</body></html>"""
    return 200, headers, cookies, body


def _resp_imperva() -> tuple[int, dict, list, str]:
    headers = {
        "x-iinfo": "test-iinfo-123",
        "x-incap-session": "test-session",
        "content-type": "text/html; charset=utf-8",
    }
    cookies = [
        "incap_ses_test_123=imp_test; Max-Age=3600; Path=/",
        "visid_incap_test=imp_v; Max-Age=3600; Path=/",
        "reese84=imp_reese; Max-Age=3600; Path=/",
    ]
    body = """<html><head><title>Mock Imperva</title></head>
<body>
<h1>Imperva Mock</h1>
<script src="incapsula.js"></script>
<img src="/utm.gif?_=1" />
<p>incapsula test</p>
</body></html>"""
    return 200, headers, cookies, body


def _resp_aws_waf() -> tuple[int, dict, list, str]:
    headers = {
        "x-amzn-waf-action": "challenge",
        "x-amz-cf-id": "test-cf-id-abc",
        "content-type": "text/html; charset=utf-8",
    }
    cookies = ["aws-waf-token=test_waf_token; Max-Age=3600; Path=/"]
    body = """<html><head><title>Mock AWS WAF</title></head>
<body>
<h1>AWS WAF Mock</h1>
<form id="aws-waf-challenge">
<input name="aws-waf-token" value="test_waf_token" />
</form>
<script src="challenge.js"></script>
</body></html>"""
    return 200, headers, cookies, body


def _resp_aws_waf_refresh() -> tuple[int, dict, list, str]:
    """Issue a NEW aws-waf-token. Simulates legitimate refresh after challenge solved."""
    _AWS_WAF_TOKEN_COUNTER["count"] += 1
    n = _AWS_WAF_TOKEN_COUNTER["count"]
    new_token = f"test_waf_token_v{n}"
    _AWS_WAF_TOKEN_COUNTER["issued"][new_token] = time.time()
    headers = {
        "x-amzn-waf-action": "challenge",
        "x-amz-cf-id": f"test-cf-id-refresh-{n}",
        "content-type": "text/json; charset=utf-8",
    }
    cookies = [f"aws-waf-token={new_token}; Max-Age=3600; Path=/"]
    body = json.dumps({
        "event": "token_refreshed",
        "token": new_token,
        "max_age_sec": 3600,
        "issued_at": _AWS_WAF_TOKEN_COUNTER["issued"][new_token],
        "previous_invalidated": list(_AWS_WAF_TOKEN_COUNTER["invalidated_tokens"]),
        "total_issued": n,
    })
    return 200, headers, cookies, body


def _resp_aws_waf_invalidate() -> tuple[int, dict, list, str]:
    """Mark the current token as invalid (server-side 401/403 or logout)."""
    # Mark all known tokens as invalidated
    for tok in _AWS_WAF_TOKEN_COUNTER["issued"]:
        _AWS_WAF_TOKEN_COUNTER["invalidated_tokens"].add(tok)
    headers = {
        "x-amzn-waf-action": "deny",
        "content-type": "text/json; charset=utf-8",
    }
    cookies = []  # server explicitly drops the cookie
    body = json.dumps({
        "event": "token_invalidated",
        "reason": "server_side_reject",
        "invalidated_count": len(_AWS_WAF_TOKEN_COUNTER["invalidated_tokens"]),
        "hint": "agent should call adapter.invalidate_token() and obtain fresh token out-of-band",
    })
    return 200, headers, cookies, body


def _resp_aws_waf_expired() -> tuple[int, dict, list, str]:
    """Return a token cookie that has already expired (Max-Age=1 second)."""
    headers = {
        "x-amzn-waf-action": "challenge",
        "content-type": "text/json; charset=utf-8",
    }
    cookies = ["aws-waf-token=expired_test_token; Max-Age=1; Path=/"]
    body = json.dumps({
        "event": "token_issued_already_expired",
        "token": "expired_test_token",
        "max_age_sec": 1,
        "hint": "agent should detect expiry and call invalidate_token('expired') or store_token() with fresh value",
    })
    return 200, headers, cookies, body


def _resp_recaptcha() -> tuple[int, dict, list, str]:
    headers = {"content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Mock reCAPTCHA</title></head>
<body>
<h1>reCAPTCHA Mock</h1>
<div class="g-recaptcha" data-sitekey="6Lc_test_key"></div>
<script src="https://www.google.com/recaptcha/api.js"></script>
<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>
</body></html>"""
    return 200, headers, cookies, body


def _resp_hcaptcha() -> tuple[int, dict, list, str]:
    headers = {"content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Mock hCaptcha</title></head>
<body>
<h1>hCaptcha Mock</h1>
<div class="h-captcha" data-sitekey="test-hc-sitekey"></div>
<script src="https://hcaptcha.com/1/api.js"></script>
<iframe src="https://hcaptcha.com/captcha/v1"></iframe>
</body></html>"""
    return 200, headers, cookies, body


def _resp_arkose() -> tuple[int, dict, list, str]:
    headers = {"content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Mock Arkose</title></head>
<body>
<h1>Arkose / FunCaptcha Mock</h1>
<div id="arkose-enforcement"></div>
<script src="https://client-api.arkoselabs.com/v2/12345/enforcement.js"></script>
<iframe src="https://funcaptcha.com/fc/v1/test"></iframe>
</body></html>"""
    return 200, headers, cookies, body


def _resp_none() -> tuple[int, dict, list, str]:
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>No Protection</title></head>
<body>
<h1>Clean Page</h1>
<p>No provider detected. Free to browse.</p>
</body></html>"""
    return 200, headers, cookies, body


def _resp_with_form() -> tuple[int, dict, list, str]:
    """A page with a form: useful for production-style fill+submit tests."""
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Login Form</title></head>
<body>
<h1>Staging Login</h1>
<form id="login" action="/login-result" method="post">
  <label>Username: <input type="text" name="username" id="u" /></label><br/>
  <label>Password: <input type="password" name="password" id="p" /></label><br/>
  <button type="submit" id="submit-btn">Sign in</button>
</form>
<p>This is staging, no real auth.</p>
</body></html>"""
    return 200, headers, cookies, body


def _resp_login_result() -> tuple[int, dict, list, str]:
    """Form submission result page."""
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = ["session=mock_session_token; Max-Age=3600; Path=/"]
    body = """<html><head><title>Welcome</title></head>
<body>
<h1>Welcome, you are signed in</h1>
<a href="/dashboard">Go to dashboard</a>
<p>Session: mock_session_token</p>
</body></html>"""
    return 200, headers, cookies, body


def _resp_dashboard() -> tuple[int, dict, list, str]:
    """Dashboard with multiple links for navigation tests."""
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Dashboard</title></head>
<body>
<h1>Dashboard</h1>
<h2>Sections</h2>
<a href="/products" id="products-link">Products</a>
<a href="/about" id="about-link">About</a>
<a href="/contact" id="contact-link">Contact</a>
<h2>Items</h2>
<ul>
<li><a href="/item/1">Item 1</a></li>
<li><a href="/item/2">Item 2</a></li>
<li><a href="/item/3">Item 3</a></li>
</ul>
</body></html>"""
    return 200, headers, cookies, body


def _resp_products() -> tuple[int, dict, list, str]:
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Products</title></head>
<body>
<h1>Products</h1>
<p>3 products in catalog</p>
<ul>
<li>Product A - $10</li>
<li>Product B - $20</li>
<li>Product C - $30</li>
</ul>
<a href="/dashboard">Back to dashboard</a>
</body></html>"""
    return 200, headers, cookies, body


def _resp_about() -> tuple[int, dict, list, str]:
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>About</title></head>
<body>
<h1>About Staging</h1>
<p>Mock site for testing.</p>
<a href="/dashboard">Back to dashboard</a>
</body></html>"""
    return 200, headers, cookies, body


def _resp_contact() -> tuple[int, dict, list, str]:
    headers = {"server": "nginx", "content-type": "text/html; charset=utf-8"}
    cookies = []
    body = """<html><head><title>Contact</title></head>
<body>
<h1>Contact</h1>
<form>
  <input type="email" name="email" id="email" placeholder="email" /><br/>
  <textarea name="msg" id="msg"></textarea><br/>
  <button type="submit">Send</button>
</form>
<a href="/dashboard">Back to dashboard</a>
</body></html>"""
    return 200, headers, cookies, body


def _resp_rotate() -> tuple[int, dict, list, str]:
    return 200, {"content-type": "application/json"}, [], json.dumps({
        "status": "rotated",
        "timestamp": time.time(),
        "message": "session state reset; force re-detect on next request",
    })


def _resp_healthz() -> tuple[int, dict, list, str]:
    return 200, {"content-type": "application/json"}, [], json.dumps({
        "status": "ok",
        "endpoints": sorted(ROUTES.keys()),
    })


ROUTES = {
    "/cloudflare": _resp_cloudflare,
    "/akamai": _resp_akamai,
    "/datadome": _resp_datadome,
    "/imperva": _resp_imperva,
    "/aws-waf": _resp_aws_waf,
    "/aws-waf/refresh": _resp_aws_waf_refresh,
    "/aws-waf/invalidate": _resp_aws_waf_invalidate,
    "/aws-waf/expired": _resp_aws_waf_expired,
    "/recaptcha": _resp_recaptcha,
    "/hcaptcha": _resp_hcaptcha,
    "/arkose": _resp_arkose,
    "/none": _resp_none,
    "/with-form": _resp_with_form,
    "/login-result": _resp_login_result,
    "/dashboard": _resp_dashboard,
    "/products": _resp_products,
    "/about": _resp_about,
    "/contact": _resp_contact,
    "/rotate": _resp_rotate,
    "/healthz": _resp_healthz,
}


# ── HTTP handler ──────────────────────────────────────────────────────
class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # quiet logging
        sys.stderr.write(f"[mock] {self.address_string()} {format % args}\n")

    def do_GET(self):
        self._serve()

    def do_POST(self):
        # for staging: accept any POST and serve the GET response
        # (so form submits "work" without backend processing)
        self._serve()

    def _serve(self):
        path = urlparse(self.path).path
        if path not in ROUTES:
            self.send_error(404, f"unknown route: {path}; try {list(ROUTES)}")
            return
        status, headers, cookies, body = ROUTES[path]()
        # status line
        self.send_response(status)
        # headers
        for k, v in headers.items():
            self.send_header(k, v)
        for c in cookies:
            self.send_header("set-cookie", c)
        self.send_header("content-length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18801)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"mock server on http://{args.host}:{args.port}")
    print("endpoints:", ", ".join(sorted(ROUTES)))
    print("ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
