"""aws_waf_token.py — AWS WAF token lifecycle management.

Scope: detection + token LIFECYCLE only. NEVER auto-obtain, NEVER fabricate.

A token is a real `aws-waf-token` cookie value issued by AWS WAF after the
client successfully solves the JS challenge. The cookie is opaque — we don't
decrypt or interpret it, just store, validate expiry, and inject it into
sessions when the adapter decides to.

This module handles:
  - Token dataclass (value, expires_at, host, source, acquired_at)
  - Persistent storage (JSON keyed by host)
  - Validation (expiry check, hostname match)
  - Legitimate invalidation (logout, server 401/403, manual flag)

This module does NOT:
  - Solve JS challenges
  - Generate or fabricate token values
  - Make network calls to AWS endpoints
  - Bypass authentication or authorization
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ── Paths ─────────────────────────────────────────────────────────────
# Storage location — keep consistent with cf_agent's other state dirs
_DEFAULT_STORE_DIR = Path(
    os.environ.get("CF_AGENT_STATE_DIR", str(Path.home() / ".cf_agent"))
)
TOKEN_STORE = _DEFAULT_STORE_DIR / "aws_waf_tokens.json"


# ── Dataclasses ───────────────────────────────────────────────────────
@dataclass
class AWSWAFToken:
    """A single AWS WAF token bound to a host.

    Fields:
      value:        opaque cookie value (the actual `aws-waf-token=...` payload)
      host:         hostname this token is valid for (exact match)
      source:       where the token came from — "browser" (read from live cookies),
                    "manual" (operator-injected), "staging" (mock/test)
      acquired_at:  ISO timestamp when we first stored this token
      expires_at:   ISO timestamp when token is no longer valid (None = unknown)
      max_age_sec:  original Max-Age in seconds (informational; expires_at is authoritative)
      invalidated:  True if marked invalid (logout, server rejection, etc.)
      invalid_reason: human-readable reason if invalidated
      notes:        free-form metadata (challenge type, fingerprint hash, etc.)
    """
    value: str
    host: str
    source: str = "manual"
    acquired_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: Optional[str] = None
    max_age_sec: int = 3600
    invalidated: bool = False
    invalid_reason: str = ""
    notes: str = ""

    # ── derived helpers ───────────────────────────────────
    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """True if past expires_at. Returns False if expires_at is None (unknown)."""
        if self.expires_at is None:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= (now or datetime.now(timezone.utc))

    def expires_in_seconds(self, now: Optional[datetime] = None) -> int:
        """Seconds until expiry. Negative = expired. None if expires_at unknown."""
        if self.expires_at is None:
            return -1
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return -1
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        delta = exp - (now or datetime.now(timezone.utc))
        return int(delta.total_seconds())

    def is_usable(self, now: Optional[datetime] = None) -> bool:
        """True if token can be used: not invalidated, not expired, host matches."""
        if self.invalidated:
            return False
        if self.is_expired(now):
            return False
        if not self.value:
            return False
        return True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AWSWAFToken":
        # tolerate extra keys gracefully
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid_keys})

    @classmethod
    def from_cookie(cls, value: str, host: str, max_age: int = 3600,
                    source: str = "browser",
                    now: Optional[datetime] = None) -> "AWSWAFToken":
        """Build a token from a raw cookie value + Set-Cookie Max-Age."""
        n = now or datetime.now(timezone.utc)
        return cls(
            value=value,
            host=host,
            source=source,
            acquired_at=n.isoformat(),
            expires_at=(n + timedelta(seconds=max_age)).isoformat(),
            max_age_sec=max_age,
        )


# ── TokenStore ────────────────────────────────────────────────────────
class AWSWAFTokenStore:
    """Persistent JSON store of AWSWAFToken, keyed by host.

    File format:
      {
        "version": 1,
        "tokens": {
          "example.com": { token_dict, ... },
          ...
        }
      }

    Multiple tokens per host are allowed (different fingerprint/source).
    Lookup prefers the most-recently-acquired usable token.
    """
    VERSION = 1

    def __init__(self, path: Path = TOKEN_STORE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[dict] = None

    # ── IO ─────────────────────────────────────────────────
    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {"version": self.VERSION, "tokens": {}}
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "tokens" not in data:
                data = {"version": self.VERSION, "tokens": {}}
        except (json.JSONDecodeError, OSError):
            data = {"version": self.VERSION, "tokens": {}}
        self._cache = data
        return data

    def _save(self) -> None:
        if self._cache is None:
            return
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as e:
            # don't crash the agent on a write failure
            print(f"[aws_waf_token] warning: failed to persist store: {e}")

    def _host_tokens(self, host: str) -> list[AWSWAFToken]:
        data = self._load()
        raw_list = data["tokens"].get(host, [])
        return [AWSWAFToken.from_dict(d) for d in raw_list]

    def _write_host_tokens(self, host: str, tokens: list[AWSWAFToken]) -> None:
        data = self._load()
        data["tokens"][host] = [t.to_dict() for t in tokens]
        self._save()

    # ── public API ────────────────────────────────────────
    def list_hosts(self) -> list[str]:
        return list(self._load()["tokens"].keys())

    def get_usable(self, host: str) -> Optional[AWSWAFToken]:
        """Return the most-recent usable token for host, or None."""
        tokens = self._host_tokens(host)
        # sort: usable first, then by acquired_at desc
        tokens.sort(
            key=lambda t: (not t.is_usable(), t.acquired_at),
            reverse=False,
        )
        for t in tokens:
            if t.is_usable():
                return t
        return None

    def get_all(self, host: str) -> list[AWSWAFToken]:
        return self._host_tokens(host)

    def store(self, token: AWSWAFToken) -> None:
        """Store a token. Appends to host's token list (no dedup by value)."""
        tokens = self._host_tokens(token.host)
        # remove any existing token with the same value (refresh case)
        tokens = [t for t in tokens if t.value != token.value]
        tokens.append(token)
        self._write_host_tokens(token.host, tokens)

    def store_from_cookie(self, value: str, host: str, max_age: int = 3600,
                          source: str = "browser",
                          notes: str = "") -> AWSWAFToken:
        """Convenience: build from cookie, store, return."""
        tok = AWSWAFToken.from_cookie(value, host, max_age, source)
        tok.notes = notes
        self.store(tok)
        return tok

    def invalidate(self, host: str, reason: str = "manual",
                   value: Optional[str] = None) -> int:
        """Mark token(s) as invalidated. Returns count of tokens touched.

        Legitimate reasons (not adversarial):
          - "logout": user logged out server-side
          - "401" / "403": server rejected the token
          - "expired_server": server signaled expiry (X-Amzn-Waf-State, etc.)
          - "manual": operator decision
          - "rotate": refresh flow replaced this token
        If value is given, only that specific token is invalidated.
        Otherwise all tokens for the host are invalidated.
        """
        tokens = self._host_tokens(host)
        n = 0
        for t in tokens:
            if value is not None and t.value != value:
                continue
            if not t.invalidated:
                t.invalidated = True
                t.invalid_reason = reason
                n += 1
        if n:
            self._write_host_tokens(host, tokens)
        return n

    def purge_expired(self, host: Optional[str] = None) -> int:
        """Remove tokens that are both expired AND invalidated. Returns count."""
        if host:
            hosts = [host]
        else:
            hosts = self.list_hosts()
        n = 0
        for h in hosts:
            tokens = self._host_tokens(h)
            keep = [t for t in tokens if not (t.is_expired() and t.invalidated)]
            n += len(tokens) - len(keep)
            if n:
                self._write_host_tokens(h, keep)
        return n

    def clear_host(self, host: str) -> int:
        """Remove ALL tokens for a host. Returns count removed."""
        tokens = self._host_tokens(host)
        n = len(tokens)
        if n:
            self._write_host_tokens(host, [])
        return n


# ── convenience singleton ────────────────────────────────────────────
_default_store: Optional[AWSWAFTokenStore] = None


def get_token_store(path: Optional[Path] = None) -> AWSWAFTokenStore:
    """Return a process-wide default store (or a custom one if path given)."""
    global _default_store
    if path is not None:
        return AWSWAFTokenStore(Path(path))
    if _default_store is None:
        _default_store = AWSWAFTokenStore()
    return _default_store
