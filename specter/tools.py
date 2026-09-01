#!/usr/bin/env python3
"""
cf_agent_tools.py — Tool layer with bypass-flag gating.

Tools split into:
  - bypass-aware:    BrowseTool, SessionTool
  - bypass-independent: ExtractTool, PlanTool, ReasoningTool

Reasoning layer is ALWAYS bypass-off. Only browser/scraping tools read
the bypass flag. The agent decides which tool to invoke; tools enforce
the flag.
"""
from __future__ import annotations

import json
import os
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
sys.path.insert(0, str(HOME))

from specter.config import BypassConfig, get_config
from specter.providers import (
    ProviderDetector, get_detector, get_registry,
    ProtectionProvider, HumanRequiredError, ProviderNotSolvableError,
    ProviderId, ChallengeState, DetectionResult,
)


# ── Tool base ─────────────────────────────────────────────────────────
class Tool(ABC):
    """Abstract tool. Subclasses declare bypass requirement."""

    name: str = "tool"
    requires_bypass: bool = False  # True = only runnable when bypass.enabled

    def __init__(self, config: BypassConfig | None = None):
        self.config = config or get_config()

    def can_run(self) -> bool:
        """Tool is runnable when config flag allows it."""
        if not self.requires_bypass:
            return True
        return self.config.bypass_enabled and self.config.authorized_test_mode

    @abstractmethod
    def run(self, **kwargs) -> Any:
        ...

    def _guard_bypass(self) -> None:
        if not self.can_run():
            raise BypassDisabledError(
                f"tool {self.name!r} requires bypass.enabled=True; "
                f"current: enabled={self.config.bypass_enabled}, "
                f"authorized_test_mode={self.config.authorized_test_mode}"
            )


# ── Exceptions ────────────────────────────────────────────────────────
class BypassDisabledError(RuntimeError):
    """Tool requires bypass but flag is OFF."""


class AuthorizationError(RuntimeError):
    """Domain not in allowed_domains."""


# ── ExtractTool (no bypass) ───────────────────────────────────────────
class ExtractTool(Tool):
    """Filter snapshot data. No I/O, no bypass."""
    name = "extract"
    requires_bypass = False

    def run(self, snap: dict, what: str = "all") -> dict:
        if not snap:
            return {"error": "no snapshot"}
        if what == "all":
            return {k: snap.get(k) for k in
                    ("title", "url", "headings", "links", "actions",
                     "forms", "inputs", "challenge", "cookies_count")}
        if what in snap:
            return {what: snap[what]}
        return {what: None}


# ── PlanTool (no bypass) ─────────────────────────────────────────────
class PlanTool(Tool):
    """Decide next action from snapshot + goal. No I/O, no bypass."""
    name = "plan"
    requires_bypass = False

    def run(self, snap: dict, goal: str, step: int = 0,
            history: list | None = None, use_llm: bool = True) -> dict:
        # delegate to cf_agent.plan_hybrid (lightweight rule + LLM)
        try:
            from specter.agent import plan_hybrid  # imported lazily to avoid cycle
        except ImportError:
            # fallback: simple rule
            return self._rule_only(snap, goal, step)
        return plan_hybrid(snap, goal, step, history or [], use_llm)

    def _rule_only(self, snap: dict, goal: str, step: int) -> dict:
        g = goal.lower()
        if "title" in g:
            return {"action": "extract", "what": "title"}
        if "heading" in g:
            return {"action": "extract", "what": "headings"}
        if "screenshot" in g:
            return {"action": "screenshot", "label": f"step{step}"}
        return {"action": "extract", "what": "all"}


# ── BrowseTool (bypass-aware) ─────────────────────────────────────────
class BrowseTool(Tool):
    """Browse a URL with auto provider detection and challenge handling.

    Flow:
      1. Guard: bypass must be enabled
      2. Authorize: domain must be in allowed_domains
      3. Optional: HTTP-level fetch to detect provider (no browser)
      4. Launch browser via cf_selenium with auto_solve=True
      5. Run detector cascade (signature → browser) on snapshot
      6. If challenge detected, look up adapter
      7. If adapter.auto_solvable: call adapter.solve(b, url)
      8. If human_required: raise HumanRequiredError
      9. Return final snapshot + detection result
    """
    name = "browse"
    requires_bypass = True

    def __init__(self, config: BypassConfig | None = None,
                 detector: ProviderDetector | None = None,
                 registry=None):
        super().__init__(config)
        self.detector = detector or get_detector()
        self.registry = registry or get_registry()

    def run(self, url: str, wait_for: str = "body",
            headless: bool | None = None) -> dict:
        self._guard_bypass()
        self._authorize(url)

        headless = self.config.headless if headless is None else headless
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""

        # step 1: HTTP-level signature detection (cheap, no browser)
        sig_result = self._signature_preflight(url, host)

        # step 2: launch browser and navigate
        from cf_selenium import Browser
        profile = f"agent_{int(time.time())}_{os.urandom(2).hex()}"
        b = Browser(profile=profile, headless=headless, auto_solve=True)
        try:
            b.get(url, wait_for=wait_for)
            snap = b.snapshot()

            # step 3: cascade to browser detection if signature was weak
            if (not sig_result or sig_result.provider == ProviderId.UNKNOWN.value
                    or sig_result.confidence < self.config.confidence_threshold):
                br = self.detector.detect_browser(snap, host=host)
                if br.confidence > sig_result.confidence:
                    sig_result = br
                # cache it
                if sig_result.host:
                    self.detector.cache.set(host, sig_result)

            # step 4: handle challenge if any
            detection = sig_result
            challenge = detection.challenge_state
            if challenge not in (ChallengeState.NONE.value, ChallengeState.UNKNOWN.value):
                adapter = self.registry.get(detection.provider)
                if adapter is not None:
                    if adapter.auto_solvable:
                        # try to solve
                        try:
                            adapter.solve(b, url)
                        except (ProviderNotSolvableError, NotImplementedError) as e:
                            # mark as unsolved but continue (b may have auto-solved)
                            snap.setdefault("_notes", []).append(
                                f"adapter {detection.provider} solve not implemented: {e}"
                            )
                    elif challenge == ChallengeState.HUMAN_REQUIRED.value:
                        # stop here
                        raise HumanRequiredError(
                            f"provider={detection.provider} requires human; "
                            f"hint: {adapter.__class__.__name__}.solve() raises HumanRequiredError",
                            provider=detection.provider,
                            hint="open the page in a real browser and complete the challenge",
                        )
            # attach detection metadata
            snap["_detection"] = detection.to_dict()
            return snap
        finally:
            try:
                b.quit()
            except Exception:
                pass

    def _signature_preflight(self, url: str, host: str) -> DetectionResult:
        """HTTP-level fetch to get headers+body for signature detection."""
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                headers = {k: r.headers.get(k) for k in r.headers.keys()}
                body = r.read().decode("utf-8", errors="ignore")[:200000]  # cap
            return self.detector.detect_signature(headers, body, host=host)
        except Exception:
            return DetectionResult(
                provider=ProviderId.UNKNOWN.value,
                confidence=0.0,
                method="signature",
                challenge_state=ChallengeState.UNKNOWN.value,
                host=host,
                evidence={"reason": "preflight fetch failed"},
            )

    def _authorize(self, url: str) -> None:
        ok, reason = self.config.check_authorization(url)
        if not ok:
            raise AuthorizationError(reason)


# ── SessionTool (bypass-aware) ────────────────────────────────────────
class SessionTool(Tool):
    """Multi-provider session management."""
    name = "session"
    requires_bypass = True

    def __init__(self, config: BypassConfig | None = None):
        super().__init__(config)
        from specter.sessions import BypassSession
        self.sessions = BypassSession()

    def run(self, action: str = "list", host: str = "",
            provider: str = ProviderId.CLOUDFLARE.value, url: str = "") -> Any:
        self._guard_bypass()
        if action == "list":
            return [r.to_dict() for r in self.sessions.list_sessions()]
        if action == "info":
            return self.sessions.info(host, provider)
        if action == "ensure":
            rec = self.sessions.ensure(host, provider)
            return rec.to_dict() if rec else None
        if action == "refresh":
            if not url:
                raise ValueError("url required for refresh")
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname or host
            rec = self.sessions.refresh_session(url, host, provider)
            return rec.to_dict()
        if action == "purge":
            return self.sessions.delete(host, provider)
        if action == "stats":
            return self.sessions.stats()
        raise ValueError(f"unknown action: {action}")


# ── Tool registry ────────────────────────────────────────────────────
TOOLS = {
    "extract": ExtractTool,
    "plan": PlanTool,
    "browse": BrowseTool,
    "session": SessionTool,
}


def get_tool(name: str, config: BypassConfig | None = None) -> Tool:
    cls = TOOLS.get(name)
    if not cls:
        raise ValueError(f"unknown tool: {name}; available: {list(TOOLS)}")
    return cls(config=config)


def list_tools() -> list[dict]:
    return [
        {"name": name, "requires_bypass": cls.requires_bypass}
        for name, cls in TOOLS.items()
    ]


# ── CLI ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list available tools")
    ap.add_argument("--tool", help="tool name")
    ap.add_argument("--action", default="run", help="tool action")
    ap.add_argument("--url", help="URL (for browse/session refresh)")
    ap.add_argument("--host", help="host (for session)")
    ap.add_argument("--provider", default=ProviderId.CLOUDFLARE.value)
    args = ap.parse_args()

    if args.list:
        for t in list_tools():
            mark = "🔓" if t["requires_bypass"] else "🔓✓"
            print(f"  {t['name']:10} requires_bypass={t['requires_bypass']}")
        sys.exit(0)

    cfg = get_config()
    print(f"config: bypass_enabled={cfg.bypass_enabled} "
          f"allowed_domains={cfg.allowed_domains} "
          f"authorized_test_mode={cfg.authorized_test_mode}")
    if args.tool:
        t = get_tool(args.tool, config=cfg)
        print(f"tool {args.tool!r}: can_run={t.can_run()}")
