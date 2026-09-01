"""hcaptcha_adapter.py — hCaptcha adapter (detection only, human required)."""
from __future__ import annotations

from .base import (
    ChallengeState,
    DetectionResult,
    HumanRequiredError,
    ProviderId,
    ProtectionProvider,
)


class HCaptchaAdapter(ProtectionProvider):
    provider_id = ProviderId.HCAPTCHA
    display_name = "hCaptcha"
    auto_solvable = False  # human required

    BODY_MARKERS = ("hcaptcha.com/1/api.js", "hcaptcha.com/captcha",
                    "h-captcha", "hcaptcha.render", "data-hcaptcha-sitekey")
    IFRAME_PATTERNS = ("hcaptcha.com/captcha/v1", "newassets.hcaptcha.com")

    def signature_detect(self, response_headers, body, host=""):
        body_low = (body or "").lower()
        evidence: dict = {}
        confidence = 0.0

        for m in self.BODY_MARKERS:
            if m in body_low:
                evidence.setdefault("body_markers", []).append(m)
                # strong marker (hcaptcha.com/*) = 0.95, weak (h-captcha, data-sitekey) = 0.85
                if m.startswith("hcaptcha.com") or m.startswith("newassets.hcaptcha.com"):
                    confidence = max(confidence, 0.95)
                else:
                    confidence = max(confidence, 0.85)

        for pat in self.IFRAME_PATTERNS:
            if pat in body_low:
                evidence.setdefault("iframes", []).append(pat)
                confidence = max(confidence, 0.9)

        if confidence <= 0:
            return self.unknown(method="signature", host=host)

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="signature",
            challenge_state=ChallengeState.HUMAN_REQUIRED,
            evidence=evidence,
            host=host,
        )

    def browser_detect(self, browser_snapshot):
        snap = browser_snapshot or {}
        html = (snap.get("html") or "").lower()
        actions = snap.get("actions", [])
        evidence: dict = {}
        confidence = 0.0

        for m in self.BODY_MARKERS:
            if m in html:
                evidence.setdefault("body_markers", []).append(m)
                # strong marker (hcaptcha.com/*) = 0.95, weak (h-captcha, data-sitekey) = 0.9
                if m.startswith("hcaptcha.com") or m.startswith("newassets.hcaptcha.com"):
                    confidence = max(confidence, 0.95)
                else:
                    confidence = max(confidence, 0.9)

        for a in actions:
            if "hcaptcha" in (a.get("selector", "") + a.get("text", "")).lower():
                evidence["hcaptcha_action"] = a.get("selector", "")
                confidence = max(confidence, 0.95)
                break

        if confidence <= 0:
            return self.unknown(method="browser", host=snap.get("url", ""))

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="browser",
            challenge_state=ChallengeState.HUMAN_REQUIRED,
            evidence=evidence,
            host=snap.get("url", ""),
        )

    def can_handle(self, state: str) -> bool:
        return state == ChallengeState.NONE

    def solve(self, browser, url: str) -> dict:
        raise HumanRequiredError(
            f"{self.display_name} requires human interaction. "
            f"Open the page in a real browser, complete the CAPTCHA, then continue.",
            provider=self.provider_id,
            hint="Find the hCaptcha checkbox ('I am human') and complete the challenge.",
        )
