"""cf_adapter.py — Cloudflare adapter (wraps cf_selenium).

This adapter does NOT reimplement any CF solver. It delegates to:
  - cf_selenium.Browser(auto_solve=True) for browser solve
  - cf_persistent.SessionDB for storage (via BypassSession)

Only adds: detection logic specific to Cloudflare signatures.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .base import (
    ChallengeState,
    DetectionResult,
    ProviderId,
    ProtectionProvider,
    SessionInfo,
)

HOME = Path(os.path.expanduser("~"))
sys.path.insert(0, str(HOME))


class CloudflareAdapter(ProtectionProvider):
    """Cloudflare detection + solve (delegates to cf_selenium)."""

    provider_id = ProviderId.CLOUDFLARE
    display_name = "Cloudflare"
    auto_solvable = True

    # signature markers
    HEADER_KEYS = {"server": ["cloudflare"], "cf-ray": None, "cf-cache-status": None}
    COOKIE_NAMES = {"cf_clearance", "__cf_bm", "cf_bm"}
    BODY_MARKERS = (
        "cf-challenge", "cf-spinner", "Attention Required! | Cloudflare",
        "cf_chl_opt", "cf_chl_cc", "__cf_chl_jschl_tk__",
        "cf-turnstile", "cf-chl-bypass",
    )

    def signature_detect(
        self, response_headers: dict, body: str, host: str = ""
    ) -> DetectionResult:
        # normalize keys to lowercase
        headers = {(k.lower() if k else ""): (v.lower() if isinstance(v, str) else "")
                   for k, v in (response_headers or {}).items()}

        evidence: dict = {}
        confidence = 0.0

        # server header
        srv = headers.get("server", "")
        if "cloudflare" in srv:
            evidence["server_header"] = srv
            confidence = max(confidence, 0.9)

        # cf-ray
        if "cf-ray" in headers:
            evidence["cf_ray"] = headers["cf-ray"]
            confidence = max(confidence, 0.85)

        # cookies (set-cookie values may be in headers as a single string or list)
        cookies_blob = " ".join(
            v for v in headers.values() if isinstance(v, str)
        ) + " " + (body or "")
        found_cookies = [c for c in self.COOKIE_NAMES if c in cookies_blob]
        if found_cookies:
            evidence["cf_cookies"] = found_cookies
            confidence = max(confidence, 0.8 if confidence == 0 else confidence + 0.1)

        # body markers
        body_low = (body or "").lower()
        found_markers = [m for m in self.BODY_MARKERS if m.lower() in body_low]
        if found_markers:
            evidence["body_markers"] = found_markers
            confidence = max(confidence, 0.85 if confidence == 0 else confidence + 0.05)

        # decide challenge state
        challenge_state = ChallengeState.NONE
        if "cf-turnstile" in body_low:
            challenge_state = ChallengeState.TURNSTILE
        elif "cf-challenge" in body_low or "cf_chl" in body_low:
            challenge_state = ChallengeState.MANAGED
        elif found_markers:
            challenge_state = ChallengeState.JS_CHALLENGE

        if confidence <= 0:
            return self.unknown(method="signature", host=host)

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="signature",
            challenge_state=challenge_state,
            evidence=evidence,
            host=host,
        )

    def browser_detect(self, browser_snapshot: dict) -> DetectionResult:
        snap = browser_snapshot or {}
        evidence: dict = {}
        confidence = 0.0

        # check cookies
        cookies = snap.get("cookies", {})
        if isinstance(cookies, dict):
            cf_cookies = [k for k in cookies if k in self.COOKIE_NAMES]
            if cf_cookies:
                evidence["cf_cookies"] = cf_cookies
                confidence = max(confidence, 0.95)
        elif isinstance(cookies, list):
            cf_cookies = [c.get("name") for c in cookies if c.get("name") in self.COOKIE_NAMES]
            if cf_cookies:
                evidence["cf_cookies"] = cf_cookies
                confidence = max(confidence, 0.95)

        # check html
        html = snap.get("html", "")
        body_markers = [m for m in self.BODY_MARKERS if m in html]
        if body_markers:
            evidence["body_markers"] = body_markers
            confidence = max(confidence, 0.9)

        # check actions / iframes
        actions = snap.get("actions", [])
        for a in actions:
            text = (a.get("text") or "").lower()
            sel = (a.get("selector") or "").lower()
            if "cf-turnstile" in sel or "turnstile" in text:
                evidence["turnstile_element"] = sel or text
                confidence = max(confidence, 0.9)
                break

        # snapshot challenge field
        challenge = snap.get("challenge", "none")
        if challenge in ("turnstile", "managed", "js_challenge"):
            evidence["snapshot_challenge"] = challenge
            confidence = max(confidence, 0.85)

        if confidence <= 0:
            return self.unknown(method="browser", host=snap.get("url", ""))

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="browser",
            challenge_state=challenge,
            evidence=evidence,
            host=snap.get("url", ""),
        )

    def can_handle(self, state: str) -> bool:
        return state in (
            ChallengeState.NONE,
            ChallengeState.TURNSTILE,
            ChallengeState.MANAGED,
            ChallengeState.JS_CHALLENGE,
        )

    def solve(self, browser, url: str) -> dict:
        """Delegate to cf_selenium.Browser for solve.

        The browser is expected to have auto_solve=True. This method just
        re-navigates to trigger the solver, then returns session data.
        """
        from cf_selenium import Browser  # local import; cf_selenium is frozen

        if browser is None:
            # caller didn't pass one; create one
            browser = Browser(profile=f"cf_{int(os.path.getmtime(__file__))}",
                             auto_solve=True)
            try:
                browser.get(url, wait_for="body")
            finally:
                # keep open; caller decides
                pass

        # extract session data
        cookies = []
        try:
            if isinstance(browser.cookies, dict):
                cookies = [{"name": k, "value": v} for k, v in browser.cookies.items()]
        except Exception:
            pass

        ua = ""
        try:
            ua = browser.execute_script("return navigator.userAgent") or ""
        except Exception:
            pass

        fp = {}
        try:
            fp = browser.execute_script(
                "return {lang:navigator.language,plat:navigator.platform,"
                "hwc:navigator.hardwareConcurrency}"
            ) or {}
        except Exception:
            pass

        cf_clearance = None
        cf_bm = None
        if isinstance(browser.cookies, dict):
            cf_clearance = browser.cookies.get("cf_clearance")
            cf_bm = browser.cookies.get("__cf_bm") or browser.cookies.get("cf_bm")

        return {
            "cookies": cookies,
            "user_agent": ua,
            "fingerprint": fp,
            "extra": {
                "cf_clearance": cf_clearance,
                "cf_bm": cf_bm,
                "ttl": 31536000 if cf_clearance else 1800,
            },
        }

    def session_info(self, host: str) -> SessionInfo:
        """Read session info via BypassSession."""
        from specter.sessions import BypassSession, ProviderId
        bs = BypassSession()
        info = bs.info(host, ProviderId.CLOUDFLARE)
        return SessionInfo(
            host=info["host"],
            provider=info["provider"],
            valid=info["valid"],
            has_clearance=info["has_cf_clearance"],
            has_bm=info["has_cf_bm"],
            expires_in=info["expires_in"],
            cookie_count=info["cookie_count"],
        )
