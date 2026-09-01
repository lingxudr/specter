"""akamai_adapter.py — Akamai Bot Manager adapter (detection only).

solve() raises ProviderNotSolvableError — Akamai Bot Manager requires
residential IP and proper sensor generation; no auto-solve in v2.
"""
from __future__ import annotations

import os
import sys
from typing import Any

from .base import (
    ChallengeState,
    DetectionResult,
    ProviderId,
    ProviderNotSolvableError,
    ProtectionProvider,
    SessionInfo,
)


class AkamaiAdapter(ProtectionProvider):
    provider_id = ProviderId.AKAMAI
    display_name = "Akamai Bot Manager"
    auto_solvable = False  # requires residential IP + sensor

    HEADER_KEYS = (
        "x-akamai-transformed", "x-acg-cache-status",
        "akamai-origin-hop", "x-akamai-request-id",
    )
    COOKIE_NAMES = ("ak_bmsc", "bm_sz", "bm_sv", "akamai*", "_abck")
    BODY_MARKERS = ("akam-test-cookie.js", "akamai", "_bmr.js", "sensor_data", "akam")

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
        for c in self.COOKIE_NAMES:
            if c in cookies_blob:
                evidence.setdefault("cookies", []).append(c)
                confidence = max(confidence, 0.8)

        body_low = (body or "").lower()
        for m in self.BODY_MARKERS:
            if m in body_low:
                evidence.setdefault("body_markers", []).append(m)
                confidence = max(confidence, 0.7)

        if confidence <= 0:
            return self.unknown(method="signature", host=host)

        challenge_state = ChallengeState.JS_CHALLENGE
        if "sensor_data" in body_low:
            challenge_state = ChallengeState.HUMAN_REQUIRED

        return DetectionResult(
            provider=self.provider_id,
            confidence=min(confidence, 1.0),
            method="signature",
            challenge_state=challenge_state,
            evidence=evidence,
            host=host,
        )

    def browser_detect(self, browser_snapshot):
        snap = browser_snapshot or {}
        html = snap.get("html", "")
        actions = snap.get("actions", [])
        evidence: dict = {}
        confidence = 0.0

        for m in self.BODY_MARKERS:
            if m in html.lower():
                evidence.setdefault("body_markers", []).append(m)
                confidence = max(confidence, 0.85)

        for a in actions:
            if "akamai" in (a.get("selector", "") + a.get("text", "")).lower():
                evidence["akamai_action"] = a.get("selector", "")
                confidence = max(confidence, 0.9)
                break

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
        return state == ChallengeState.NONE  # only NONE we can "handle" (passthrough)

    def solve(self, browser, url: str) -> dict:
        raise ProviderNotSolvableError(
            f"{self.display_name} requires residential IP and proper sensor generation; "
            f"no auto-solve available. Use a commercial solver or human intervention.",
        )
