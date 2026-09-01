"""aws_waf_adapter.py — AWS WAF / Shield adapter.

Scope: detection + token LIFECYCLE only. NEVER auto-obtain tokens.

Detection (signature + browser) reads `aws-waf-token` cookies and AWS WAF
challenge headers. The token lifecycle is exposed via:
  - token_state(host)  → current usable token + metadata
  - load_token(host)   → read from store
  - store_token(...)   → persist a real token (browser-obtained or operator)
  - invalidate_token(host, reason) → mark invalid (logout/401/manual/rotate)
  - refresh_token(host)  → remove old token, force re-acquisition
  - solve()           → NOT auto-obtain. If token missing/expired → raise
                        HumanRequiredError. If token present → return cookie
                        dict so the session layer can apply it.

This module does NOT:
  - Solve JS challenges
  - Generate or fabricate token values
  - Make network calls to AWS endpoints
  - Bypass authentication or authorization
"""
from __future__ import annotations

import logging
from typing import Optional

from .aws_waf_token import AWSWAFToken, AWSWAFTokenStore, get_token_store
from .base import (
    ChallengeState,
    DetectionResult,
    HumanRequiredError,
    ProviderId,
    ProviderNotSolvableError,
    ProtectionProvider,
    SessionInfo,
)

_log = logging.getLogger("cf_agent.aws_waf")


class AWSWAFAdapter(ProtectionProvider):
    provider_id = ProviderId.AWS_WAF
    display_name = "AWS WAF / Shield"
    auto_solvable = False  # tokens are obtained out-of-band; we don't auto-solve

    COOKIE_NAMES = ("aws-waf-token",)
    HEADER_KEYS = ("x-amzn-waf-action", "x-amz-cf-id", "x-amz-waf-id")
    BODY_MARKERS = ("awswaf", "aws-waf-token", "challenge.js", "WAF")

    def __init__(self, store: Optional[AWSWAFTokenStore] = None):
        super().__init__()
        self._store = store  # lazy-init if None

    @property
    def store(self) -> AWSWAFTokenStore:
        if self._store is None:
            self._store = get_token_store()
        return self._store

    # ── detection (preserved from v2; tested in test_prod_style.py) ──
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
                confidence = max(confidence, 0.9)

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

        for c in self.COOKIE_NAMES:
            if c in cookie_str:
                evidence.setdefault("cookies", []).append(c)
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
        # We never auto-solve, but we handle both states (NONE if token present,
        # JS_CHALLENGE if token missing).
        return state in (ChallengeState.NONE, ChallengeState.JS_CHALLENGE)

    # ── token lifecycle (NEW for v3) ────────────────────────────────
    def token_state(self, host: str) -> dict:
        """Return current token state for a host.

        Shape:
          {
            "host": str,
            "has_usable_token": bool,
            "token": Optional[TokenDict],
            "total_stored": int,
            "expired_count": int,
            "invalidated_count": int,
            "needs_refresh": bool,
          }
        """
        all_tokens = self.store.get_all(host)
        usable = self.store.get_usable(host)
        now_expired = sum(1 for t in all_tokens if t.is_expired())
        now_invalid = sum(1 for t in all_tokens if t.invalidated)
        return {
            "host": host,
            "has_usable_token": usable is not None,
            "token": usable.to_dict() if usable else None,
            "total_stored": len(all_tokens),
            "expired_count": now_expired,
            "invalidated_count": now_invalid,
            "needs_refresh": usable is None,
        }

    def load_token(self, host: str) -> Optional[AWSWAFToken]:
        """Return usable token or None. Does NOT generate one."""
        return self.store.get_usable(host)

    def store_token(self, value: str, host: str, max_age: int = 3600,
                     source: str = "browser", notes: str = "") -> AWSWAFToken:
        """Persist a real token obtained out-of-band (browser or operator).

        `source` indicates origin: "browser" (read from live cf_selenium cookies),
        "manual" (operator-injected for staging), "staging" (mock-only).
        """
        if not value or not isinstance(value, str):
            raise ValueError("token value must be a non-empty string")
        if not host or not isinstance(host, str):
            raise ValueError("host must be a non-empty string")
        tok = self.store.store_from_cookie(
            value=value, host=host, max_age=max_age,
            source=source, notes=notes,
        )
        _log.info(f"aws-waf token stored: host={host} source={source} "
                  f"expires_at={tok.expires_at}")
        return tok

    def invalidate_token(self, host: str, reason: str = "manual",
                         value: Optional[str] = None) -> int:
        """Mark token(s) as invalid. Returns count invalidated.

        Legitimate reasons: 'logout', '401', '403', 'expired_server',
        'rotate', 'manual'. See AWSWAFTokenStore.invalidate for details.
        """
        n = self.store.invalidate(host, reason=reason, value=value)
        _log.info(f"aws-waf token invalidated: host={host} reason={reason} "
                  f"count={n}")
        return n

    def refresh_token(self, host: str) -> int:
        """Invalidate all tokens for a host. Caller must obtain a fresh one
        out-of-band (browser, operator, or legitimate provider endpoint).
        Returns count invalidated.
        """
        return self.invalidate_token(host, reason="rotate")

    def purge_expired(self, host: Optional[str] = None) -> int:
        """Drop tokens that are both expired AND invalidated. Returns count."""
        return self.store.purge_expired(host)

    # ── solve (NEVER auto-obtain) ─────────────────────────────────
    def solve(self, browser, url: str) -> dict:
        """Apply existing token to session OR raise HumanRequiredError.

        Returns dict: {cookies: {name: value}, expires_in: int, source: str}
        Raises:
          HumanRequiredError: if no usable token — operator/browser must obtain one.
          ProviderNotSolvableError: if challenge state is non-JS (shouldn't happen).
        """
        from urllib.parse import urlparse
        host = urlparse(url).hostname if url else ""
        if not host:
            raise HumanRequiredError(
                f"{self.display_name}: cannot derive host from url={url!r}",
                provider=self.provider_id,
                hint="pass a valid URL with a hostname",
            )

        token = self.load_token(host)
        if token is None:
            # check WHY no token — gives a precise hint
            state = self.token_state(host)
            if state["total_stored"] == 0:
                hint = (
                    f"no aws-waf-token stored for {host}; "
                    f"solve the challenge in a real browser once and store the "
                    f"resulting cookie via store_token()"
                )
            elif state["expired_count"] > 0 and state["invalidated_count"] == 0:
                hint = (
                    f"aws-waf-token for {host} has expired; "
                    f"obtain a fresh token in a real browser"
                )
            else:
                hint = (
                    f"all aws-waf tokens for {host} are invalidated "
                    f"(reason: {self.store.get_all(host)[0].invalid_reason or 'unknown'}); "
                    f"obtain a fresh token"
                )
            raise HumanRequiredError(
                f"{self.display_name}: no usable token for {host}",
                provider=self.provider_id,
                hint=hint,
            )

        # token exists — return cookie dict for session layer to apply
        return {
            "cookies": {"aws-waf-token": token.value},
            "expires_in": token.expires_in_seconds(),
            "source": token.source,
            "host": host,
            "token_acquired_at": token.acquired_at,
            "notes": (
                "Token was applied to the session. solve() did NOT obtain or "
                "modify the token. If the server rejects this token, call "
                "invalidate_token() and obtain a fresh one out-of-band."
            ),
        }

    # ── session info (extended) ───────────────────────────────────
    def session_info(self, host: str) -> SessionInfo:
        """Read token state via TokenStore, surface to agent."""
        from datetime import datetime, timezone
        state = self.token_state(host)
        token_dict = state["token"]
        expires_in = -1
        if token_dict and token_dict.get("expires_at"):
            try:
                exp = datetime.fromisoformat(token_dict["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expires_in = int((exp - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
        return SessionInfo(
            host=host,
            provider=self.provider_id,
            valid=state["has_usable_token"],
            has_clearance=False,  # AWS WAF doesn't have a "clearance" cookie like CF
            has_bm=state["has_usable_token"],  # we use this field for "has token"
            expires_in=expires_in,
            cookie_count=state["total_stored"],
        )
