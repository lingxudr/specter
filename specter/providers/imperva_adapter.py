"""imperva_adapter.py — Imperva / Incapsula adapter (detection only)."""
from __future__ import annotations

import re
from .base import (
    ChallengeState,
    DetectionResult,
    ProviderId,
    ProviderNotSolvableError,
    ProtectionProvider,
)


class ImpervaAdapter(ProtectionProvider):
    provider_id = ProviderId.IMPERVA
    display_name = "Imperva / Incapsula"
    auto_solvable = False

    HEADER_KEYS = ("x-iinfo", "x-incap-session")
    COOKIE_PATTERNS = (r"incap_ses_[^=]+", r"visid_incap_[^=]+", r"reese84")
    BODY_MARKERS = ("incapsula", "imperva", "_Incapsula_Resource", "utm.gif", "reese84")

    def signature_detect(self, response_headers, body, host=""):
        headers = {(k.lower() if k else ""): (str(v).lower() if v else "")
                   for k, v in (response_headers or {}).items()}
        evidence: dict = {}
        confidence = 0.0

        for hk in self.HEADER_KEYS:
            if hk in headers:
                evidence[hk] = headers[hk]
                confidence = max(confidence, 0.85)

        cookies_blob = " ".join(str(v) for v in headers.values()) + " " + (body or "")
        for pat in self.COOKIE_PATTERNS:
            if re.search(pat, cookies_blob):
                evidence.setdefault("cookies", []).append(pat)
                confidence = max(confidence, 0.85)

        body_low = (body or "").lower()
        for m in self.BODY_MARKERS:
            if m in body_low:
                evidence.setdefault("body_markers", []).append(m)
                confidence = max(confidence, 0.7)

        if confidence <= 0:
            return self.unknown(method="signature", host=host)

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="signature",
            challenge_state=ChallengeState.JS_CHALLENGE,
            evidence=evidence,
            host=host,
        )

    def browser_detect(self, browser_snapshot):
        snap = browser_snapshot or {}
        cookies = snap.get("cookies", {})
        html = (snap.get("html") or "").lower()
        evidence: dict = {}
        confidence = 0.0

        cookie_str = ""
        if isinstance(cookies, dict):
            cookie_str = " ".join(cookies.keys())
        elif isinstance(cookies, list):
            cookie_str = " ".join(c.get("name", "") for c in cookies)

        for pat in self.COOKIE_PATTERNS:
            if re.search(pat, cookie_str):
                evidence.setdefault("cookies", []).append(pat)
                confidence = max(confidence, 0.9)

        for m in self.BODY_MARKERS:
            if m in html:
                evidence.setdefault("body_markers", []).append(m)
                confidence = max(confidence, 0.85)

        if confidence <= 0:
            return self.unknown(method="browser", host=snap.get("url", ""))

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="browser",
            challenge_state=ChallengeState.JS_CHALLENGE,
            evidence=evidence,
            host=snap.get("url", ""),
        )

    def can_handle(self, state: str) -> bool:
        return state == ChallengeState.NONE

    def solve(self, browser, url: str) -> dict:
        raise ProviderNotSolvableError(
            f"{self.display_name} requires commercial solver + residential IP; "
            f"no auto-solve available.",
        )
