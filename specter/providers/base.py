"""base.py — ProtectionProvider ABC + enums + dataclasses.

Defines the abstract interface every provider adapter must implement.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ── Enums ─────────────────────────────────────────────────────────────
class ChallengeState(str, Enum):
    """State of a protection challenge on a page."""
    NONE = "none"
    JS_CHALLENGE = "js_challenge"
    CAPTCHA = "captcha"
    TURNSTILE = "turnstile"
    MANAGED = "managed"
    HUMAN_REQUIRED = "human_required"
    UNKNOWN = "unknown"


class ProviderId(str, Enum):
    """8 named providers + 1 unknown sentinel = 9 enum values total.

    8 named: cloudflare, akamai, datadome, imperva, aws_waf, recaptcha, hcaptcha, arkose
    1 sentinel: unknown (used when detection confidence < threshold; NEVER registered)
    """
    CLOUDFLARE = "cloudflare"
    AKAMAI = "akamai"
    DATADOME = "datadome"
    IMPERVA = "imperva"
    AWS_WAF = "aws_waf"
    RECAPTCHA = "recaptcha"
    HCAPTCHA = "hcaptcha"
    ARKOSE = "arkose"
    UNKNOWN = "unknown"

    @classmethod
    def all_named(cls) -> list[str]:
        return [cls.CLOUDFLARE.value, cls.AKAMAI.value, cls.DATADOME.value,
                cls.IMPERVA.value, cls.AWS_WAF.value, cls.RECAPTCHA.value,
                cls.HCAPTCHA.value, cls.ARKOSE.value]

    @classmethod
    def is_valid(cls, pid: str) -> bool:
        return pid in cls.all_named() or pid == cls.UNKNOWN.value


# ── Data classes ──────────────────────────────────────────────────────
@dataclass
class DetectionResult:
    """Result of a provider-detection attempt.

    JSON-serializable via to_dict().
    """
    provider: str  # ProviderId value
    confidence: float  # 0.0..1.0
    method: str  # "signature" | "browser" | "cached"
    challenge_state: str  # ChallengeState value
    evidence: dict = field(default_factory=dict)  # matching headers/cookies/selectors
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    host: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "DetectionResult":
        return cls(**d)

    @classmethod
    def from_json(cls, s: str) -> "DetectionResult":
        return cls.from_dict(json.loads(s))

    def to_provider_enum(self) -> ProviderId:
        return ProviderId(self.provider)

    def to_state_enum(self) -> ChallengeState:
        return ChallengeState(self.challenge_state)


@dataclass
class SessionInfo:
    """Lightweight session metadata returned by adapter.session_info()."""
    host: str
    provider: str
    valid: bool
    has_clearance: bool = False
    has_bm: bool = False
    expires_in: int = -1
    cookie_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Exceptions ────────────────────────────────────────────────────────
class HumanRequiredError(RuntimeError):
    """Raised by an adapter when the challenge requires a human to solve
    (reCAPTCHA, hCaptcha, Arkose). The agent should stop and request
    user intervention, not retry."""

    def __init__(self, message: str, provider: str, hint: str = ""):
        super().__init__(message)
        self.provider = provider
        self.hint = hint


class ProviderNotSolvableError(NotImplementedError):
    """Raised by an adapter when solve() is called but the provider is
    not auto-solvable (requires commercial solver, residential IP, etc.)."""


# ── Abstract base ─────────────────────────────────────────────────────
class ProtectionProvider(ABC):
    """Abstract base for all protection-provider adapters.

    Subclasses MUST set:
      - provider_id: ProviderId enum value
      - display_name: human-readable name
      - auto_solvable: True if solve() can complete the challenge
    """

    provider_id: str = ""  # set by subclass
    display_name: str = ""  # set by subclass
    auto_solvable: bool = False  # set by subclass

    @abstractmethod
    def signature_detect(
        self, response_headers: dict, body: str, host: str = ""
    ) -> DetectionResult:
        """First-pass detection: signature/header/cookie only. No browser.

        Returns DetectionResult with confidence in 0.0..1.0.
        Should be fast and side-effect-free.
        """

    @abstractmethod
    def browser_detect(self, browser_snapshot: dict) -> DetectionResult:
        """Second-pass detection: called when signature is ambiguous.

        browser_snapshot is the dict from cf_selenium.Browser.snapshot().
        Looks for provider-specific elements (iframes, scripts, cookies).
        """

    @abstractmethod
    def solve(self, browser, url: str) -> dict:
        """Attempt to auto-solve the challenge and return session cookies.

        For non-auto-solvable providers, raises ProviderNotSolvableError
        or HumanRequiredError (with hint about why).

        Returns dict with keys: cookies, user_agent, fingerprint, extra
        """

    @abstractmethod
    def can_handle(self, state: str) -> bool:
        """Whether this adapter can handle the given challenge state.

        state: ChallengeState value
        Returns True if solve() is applicable.
        """

    def session_info(self, host: str) -> SessionInfo:
        """Default: returns unavailable. Adapters can override."""
        return SessionInfo(host=host, provider=self.provider_id, valid=False)

    # ── convenience ─────────────────────────────────────
    def unknown(self, method: str = "signature", host: str = "") -> DetectionResult:
        """Helper to return an UNKNOWN detection result."""
        return DetectionResult(
            provider=ProviderId.UNKNOWN,
            confidence=0.0,
            method=method,
            challenge_state=ChallengeState.UNKNOWN,
            host=host,
            evidence={"reason": f"no markers found for {self.display_name}"},
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.provider_id} auto_solvable={self.auto_solvable}>"
