"""datadome_adapter.py — DataDome adapter (detection only)."""
from __future__ import annotations

from .base import (
    ChallengeState,
    DetectionResult,
    ProviderId,
    ProviderNotSolvableError,
    ProtectionProvider,
)


class DataDomeAdapter(ProtectionProvider):
    provider_id = ProviderId.DATADOME
    display_name = "DataDome"
    auto_solvable = False

    COOKIE_NAMES = ("datadome",)
    BODY_MARKERS = ("datadome.com/captcha", "datadome.js", "dd.captcha.js",
                    "datadome.com/tag.js", "dd.js")
    HEADER_KEYS = ("x-datadome-clientid", "x-datadome-campaignid")

    def signature_detect(self, response_headers, body, host=""):
        headers = {(k.lower() if k else ""): (str(v).lower() if v else "")
                   for k, v in (response_headers or {}).items()}
        evidence: dict = {}
        confidence = 0.0

        for hk in self.HEADER_KEYS:
            if hk in headers:
                evidence[hk] = headers[hk]
                confidence = max(confidence, 0.9)

        cookies_blob = " ".join(str(v) for v in headers.values()) + " " + (body or "")
        for c in self.COOKIE_NAMES:
            if c in cookies_blob:
                evidence.setdefault("cookies", []).append(c)
                confidence = max(confidence, 0.85)

        body_low = (body or "").lower()
        for m in self.BODY_MARKERS:
            if m in body_low:
                evidence.setdefault("body_markers", []).append(m)
                confidence = max(confidence, 0.8)

        if confidence <= 0:
            return self.unknown(method="signature", host=host)

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="signature",
            challenge_state=ChallengeState.CAPTCHA,
            evidence=evidence,
            host=host,
        )

    def browser_detect(self, browser_snapshot):
        snap = browser_snapshot or {}
        cookies = snap.get("cookies", {})
        evidence: dict = {}
        confidence = 0.0

        if isinstance(cookies, dict):
            for c in self.COOKIE_NAMES:
                if c in cookies:
                    evidence.setdefault("cookies", []).append(c)
                    confidence = max(confidence, 0.9)
        elif isinstance(cookies, list):
            for c in cookies:
                if c.get("name") in self.COOKIE_NAMES:
                    evidence.setdefault("cookies", []).append(c.get("name"))
                    confidence = max(confidence, 0.9)

        html = (snap.get("html") or "").lower()
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
            challenge_state=ChallengeState.CAPTCHA,
            evidence=evidence,
            host=snap.get("url", ""),
        )

    def can_handle(self, state: str) -> bool:
        return state == ChallengeState.NONE

    def solve(self, browser, url: str) -> dict:
        raise ProviderNotSolvableError(
            f"{self.display_name} requires commercial solver or specialized tool; "
            f"no auto-solve available.",
        )
