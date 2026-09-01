"""recaptcha_adapter.py — Google reCAPTCHA adapter (detection only, human required)."""
from __future__ import annotations

from .base import (
    ChallengeState,
    DetectionResult,
    HumanRequiredError,
    ProviderId,
    ProtectionProvider,
)


class ReCaptchaAdapter(ProtectionProvider):
    provider_id = ProviderId.RECAPTCHA
    display_name = "Google reCAPTCHA"
    auto_solvable = False  # human required by design

    BODY_MARKERS = ("google.com/recaptcha", "grecaptcha", "recaptcha/api2",
                    "recaptcha/api.js", "grecaptcha.render")
    IFRAME_PATTERNS = ("recaptcha/api2/anchor", "recaptcha/api2/bframe")

    def signature_detect(self, response_headers, body, host=""):
        headers = {(k.lower() if k else ""): (str(v).lower() if v else "")
                   for k, v in (response_headers or {}).items()}
        body_low = (body or "").lower()
        evidence: dict = {}
        confidence = 0.0

        for m in self.BODY_MARKERS:
            if m in body_low:
                evidence.setdefault("body_markers", []).append(m)
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
                confidence = max(confidence, 0.9)

        for a in actions:
            if "recaptcha" in (a.get("selector", "") + a.get("text", "")).lower():
                evidence["recaptcha_action"] = a.get("selector", "")
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
        return state == ChallengeState.NONE  # only NONE; CAPTCHA = stop

    def solve(self, browser, url: str) -> dict:
        raise HumanRequiredError(
            f"{self.display_name} requires human interaction. "
            f"Open the page in a real browser, complete the CAPTCHA, then continue.",
            provider=self.provider_id,
            hint="Find the 'I'm not a robot' checkbox or image challenge and click solve.",
        )
