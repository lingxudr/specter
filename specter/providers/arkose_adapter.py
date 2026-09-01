"""arkose_adapter.py — Arkose Labs / FunCaptcha adapter (detection only, human required)."""
from __future__ import annotations

from .base import (
    ChallengeState,
    DetectionResult,
    HumanRequiredError,
    ProviderId,
    ProtectionProvider,
)


class ArkoseAdapter(ProtectionProvider):
    provider_id = ProviderId.ARKOSE
    display_name = "Arkose Labs / FunCaptcha"
    auto_solvable = False  # advanced puzzle; human or specialized ML

    BODY_MARKERS = ("arkoselabs.com/v2/", "client-api.arkoselabs.com",
                    "funcaptcha.com/fc", "arkoselabs.com", "arkose.enforcement")
    IFRAME_PATTERNS = ("funcaptcha.com/fc/v1", "client-api.arkoselabs.com/fc/")

    def signature_detect(self, response_headers, body, host=""):
        body_low = (body or "").lower()
        evidence: dict = {}
        confidence = 0.0

        for m in self.BODY_MARKERS:
            if m in body_low:
                evidence.setdefault("body_markers", []).append(m)
                confidence = max(confidence, 0.9)

        for pat in self.IFRAME_PATTERNS:
            if pat in body_low:
                evidence.setdefault("iframes", []).append(pat)
                confidence = max(confidence, 0.95)

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
            if any(x in (a.get("selector", "") + a.get("text", "")).lower()
                   for x in ("arkose", "funcaptcha")):
                evidence["arkose_action"] = a.get("selector", "")
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
            f"{self.display_name} requires human interaction or specialized ML solver. "
            f"Open the page in a real browser, complete the puzzle, then continue.",
            provider=self.provider_id,
            hint="Solve the image rotation puzzle (rotate objects to match reference) or audio challenge.",
        )
