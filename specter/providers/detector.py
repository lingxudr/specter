"""detector.py — Multi-strategy provider detection with caching.

Cascade:
  1. detect_signature(headers, body) — fast, header/cookie/script markers
  2. detect_browser(snapshot)        — if signature confidence < threshold
  3. detect(headers, body, snapshot) — orchestrator with cache

Cache: DetectionCache, TTL configurable (default 300s).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .base import ChallengeState, DetectionResult, ProviderId, ProtectionProvider
from .registry import ProviderRegistry, get_registry


# ── Cache ─────────────────────────────────────────────────────────────
@dataclass
class _CacheEntry:
    result: DetectionResult
    timestamp: float


class DetectionCache:
    """Per-host detection cache with TTL."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}

    def get(self, host: str) -> DetectionResult | None:
        entry = self._store.get(host)
        if not entry:
            return None
        if (time.time() - entry.timestamp) > self.ttl:
            self._store.pop(host, None)
            return None
        # return a copy with method="cached" to indicate cache hit
        return DetectionResult(
            provider=entry.result.provider,
            confidence=entry.result.confidence,
            method="cached",
            challenge_state=entry.result.challenge_state,
            evidence=entry.result.evidence,
            timestamp=entry.result.timestamp,
            host=entry.result.host,
        )

    def set(self, host: str, result: DetectionResult) -> None:
        self._store[host] = _CacheEntry(result=result, timestamp=time.time())

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ── Detector ──────────────────────────────────────────────────────────
class ProviderDetector:
    """Multi-strategy provider detection.

    Confidence threshold: 0.6 default. Below that, returns UNKNOWN.
    Browser second-pass: triggered if signature confidence < 0.8 (configurable).
    """

    def __init__(self, registry: ProviderRegistry | None = None,
                 cache_ttl_seconds: int = 300,
                 confidence_threshold: float = 0.6,
                 browser_cascade_threshold: float = 0.8):
        self.registry = registry or get_registry()
        self.cache = DetectionCache(ttl_seconds=cache_ttl_seconds)
        self.confidence_threshold = confidence_threshold
        self.browser_cascade_threshold = browser_cascade_threshold

    def detect_signature(self, headers: dict, body: str, host: str = "") -> DetectionResult:
        """Run all providers' signature_detect(); return highest confidence."""
        best: DetectionResult | None = None
        for prov in self.registry.all_providers():
            try:
                r = prov.signature_detect(headers or {}, body or "", host=host)
            except Exception:
                continue
            if r.confidence <= 0:
                continue
            if best is None or r.confidence > best.confidence:
                best = r
        if best is None or best.confidence < self.confidence_threshold:
            return DetectionResult(
                provider=ProviderId.UNKNOWN,
                confidence=0.0,
                method="signature",
                challenge_state=ChallengeState.UNKNOWN,
                host=host,
                evidence={"reason": "no provider above confidence threshold"},
            )
        if host:
            best.host = host
        return best

    def detect_browser(self, snapshot: dict, host: str = "") -> DetectionResult:
        """Run all providers' browser_detect(); return highest confidence."""
        if not snapshot:
            return DetectionResult(
                provider=ProviderId.UNKNOWN,
                confidence=0.0,
                method="browser",
                challenge_state=ChallengeState.UNKNOWN,
                host=host,
                evidence={"reason": "no snapshot provided"},
            )
        best: DetectionResult | None = None
        for prov in self.registry.all_providers():
            try:
                r = prov.browser_detect(snapshot)
            except Exception:
                continue
            if r.confidence <= 0:
                continue
            if best is None or r.confidence > best.confidence:
                best = r
        if best is None or best.confidence < self.confidence_threshold:
            return DetectionResult(
                provider=ProviderId.UNKNOWN,
                confidence=0.0,
                method="browser",
                challenge_state=ChallengeState.UNKNOWN,
                host=host,
                evidence={"reason": "no provider above threshold (browser)"},
            )
        if host:
            best.host = host
        return best

    def detect(
        self,
        headers: dict | None = None,
        body: str = "",
        snapshot: dict | None = None,
        host: str = "",
    ) -> DetectionResult:
        """Orchestrator: cache → signature → browser (if needed)."""
        if host:
            cached = self.cache.get(host)
            if cached:
                return cached

        sig = self.detect_signature(headers or {}, body, host=host)
        if sig.confidence >= self.browser_cascade_threshold:
            self.cache.set(host, sig) if host else None
            return sig

        # cascade to browser if signature is weak and snapshot available
        if snapshot and sig.confidence < self.browser_cascade_threshold:
            br = self.detect_browser(snapshot, host=host)
            # take whichever is higher confidence
            chosen = br if br.confidence > sig.confidence else sig
            self.cache.set(host, chosen) if host else None
            return chosen

        # signature only
        if host and sig.provider != ProviderId.UNKNOWN.value:
            self.cache.set(host, sig)
        return sig

    def invalidate(self, host: str) -> None:
        self.cache._store.pop(host, None)


_singleton: ProviderDetector | None = None


def get_detector() -> ProviderDetector:
    """Get the global detector singleton (default config)."""
    global _singleton
    if _singleton is None:
        _singleton = ProviderDetector()
    return _singleton


def reset_detector() -> None:
    global _singleton
    _singleton = None
