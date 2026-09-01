#!/usr/bin/env python3
"""
cf_agent_sessions.py — Multi-provider session storage wrapper.

Wraps cf_persistent.SessionDB (frozen dependency) and namespaces by
provider:host, so cloudflare:nowsecure.nl and akamai:nowsecure.nl are
stored separately. Provider-specific metadata goes into the record's
extra JSON field (handled by caller; SessionDB stores core fields).

This module does NOT reimplement any solver. It only provides:
  - key translation (provider, host) -> namespaced host
  - session CRUD with provider context
  - human-friendly list_sessions() grouping by provider
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HOME = Path(os.path.expanduser("~"))
sys.path.insert(0, str(HOME))

# Import the FROZEN cf_persistent module
try:
    from cf_persistent import SessionDB
except ImportError as e:
    print(f"FATAL: cf_persistent import failed: {e}", file=sys.stderr)
    raise


# ── ProviderId stub (will move to cf_agent_providers.base later) ─────
class ProviderId:
    """Stub enum-like. Full enum defined in cf_agent_providers.base."""
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
        return [cls.CLOUDFLARE, cls.AKAMAI, cls.DATADOME, cls.IMPERVA,
                cls.AWS_WAF, cls.RECAPTCHA, cls.HCAPTCHA, cls.ARKOSE]

    @classmethod
    def is_valid(cls, pid: str) -> bool:
        return pid in cls.all_named() or pid == cls.UNKNOWN


# ── Data classes ──────────────────────────────────────────────────────
@dataclass
class SessionRecord:
    """Provider-namespaced session record returned by get/ensure."""
    provider: str
    host: str
    user_agent: str = ""
    fingerprint: dict = field(default_factory=dict)
    cf_clearance: str | None = None
    cf_clearance_expires: int = 0
    cf_bm: str | None = None
    cookies: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # provider-specific metadata
    challenge_count: int = 0
    success_count: int = 0
    last_used: int = 0
    created_at: int = 0

    def is_valid(self) -> bool:
        """Session is valid if cf_clearance or cf_bm is set and not expired."""
        now = int(time.time())
        if self.cf_clearance and self.cf_clearance_expires > now:
            return True
        if self.cf_bm and self.cf_clearance_expires > now - 1500:  # cf_bm 30min default
            return True
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["is_valid"] = self.is_valid()
        return d


# ── BypassSession wrapper ────────────────────────────────────────────
class BypassSession:
    """Multi-provider session manager.

    Storage: cf_persistent.SessionDB (FROZEN, no changes).
    Namespacing: key format is `provider:host` stored in `host` column.
    """

    NS_SEP = ":"

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = HOME / ".cf_agent" / "sessions.db"
        db_path = Path(os.path.expanduser(str(db_path)))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = SessionDB(db_path)
        self._extra_store_path = db_path.parent / "extras.json"
        self._extras: dict[str, dict] = self._load_extras()

    # ── key translation ────────────────────────────────
    @classmethod
    def _ns_host(cls, provider: str, host: str) -> str:
        """Build namespaced key: 'cloudflare:nowsecure.nl'."""
        if provider == ProviderId.UNKNOWN:
            raise ValueError("cannot namespace UNKNOWN provider")
        if not ProviderId.is_valid(provider):
            raise ValueError(f"invalid provider: {provider!r}")
        if not host:
            raise ValueError("host required")
        return f"{provider}{cls.NS_SEP}{host}"

    @classmethod
    def _unns_host(cls, ns: str) -> tuple[str, str]:
        """Split namespaced key into (provider, host)."""
        if cls.NS_SEP not in ns:
            return (ProviderId.UNKNOWN, ns)
        provider, host = ns.split(cls.NS_SEP, 1)
        return (provider, host)

    # ── extras store (provider-specific metadata) ───────
    def _load_extras(self) -> dict[str, dict]:
        if self._extra_store_path.exists():
            try:
                return json.loads(self._extra_store_path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_extras(self) -> None:
        try:
            self._extra_store_path.write_text(json.dumps(self._extras, indent=2))
        except OSError:
            pass

    # ── CRUD ───────────────────────────────────────────
    def get(self, host: str, provider: str) -> SessionRecord | None:
        """Get a single session by host+provider."""
        ns = self._ns_host(provider, host)
        row = self.db.get_session(ns)
        if not row:
            return None
        cookies = self.db.get_cookies(ns)
        rec = SessionRecord(
            provider=provider,
            host=host,
            user_agent=row.get("user_agent", ""),
            fingerprint=json.loads(row.get("fingerprint", "{}") or "{}"),
            cf_clearance=row.get("cf_clearance"),
            cf_clearance_expires=row.get("cf_clearance_expires") or 0,
            cf_bm=row.get("cf_bm"),
            cookies=cookies,
            extra=self._extras.get(ns, {}),
            challenge_count=row.get("challenge_count", 0),
            success_count=row.get("success_count", 0),
            last_used=row.get("last_used", 0),
            created_at=row.get("created_at", 0),
        )
        return rec

    def ensure(self, host: str, provider: str) -> SessionRecord | None:
        """Return valid session if exists and not expired, else None.

        Caller should call refresh_session() if None returned.
        """
        rec = self.get(host, provider)
        if rec and rec.is_valid():
            return rec
        return None

    def save(
        self,
        host: str,
        provider: str,
        user_agent: str,
        fingerprint: dict,
        cf_clearance: str | None = None,
        cf_bm: str | None = None,
        cookies: list | None = None,
        extra: dict | None = None,
        cf_clearance_ttl: int = 1800,
    ) -> SessionRecord:
        """Save session for (host, provider). Replaces any existing record."""
        ns = self._ns_host(provider, host)
        self.db.save_session(
            host=ns,
            ua=user_agent,
            fp=fingerprint,
            cf_clearance=cf_clearance,
            cf_bm=cf_bm,
            cf_clearance_ttl=cf_clearance_ttl,
        )
        if cookies:
            self.db.save_cookies(ns, cookies)
        if extra:
            self._extras[ns] = extra
            self._save_extras()
        rec = self.get(host, provider)
        assert rec is not None  # just saved
        return rec

    def delete(self, host: str, provider: str) -> bool:
        """Purge session. Returns True if a record was deleted."""
        ns = self._ns_host(provider, host)
        # delete cookies
        try:
            import sqlite3
            with sqlite3.connect(self.db.path) as c:
                c.execute("DELETE FROM cookies WHERE host=?", (ns,))
                c.execute("DELETE FROM sessions WHERE host=?", (ns,))
        except Exception:
            pass
        if ns in self._extras:
            del self._extras[ns]
            self._save_extras()
        return True

    def list_sessions(self, provider: str | None = None) -> list[SessionRecord]:
        """List sessions, optionally filtered by provider. Returns full records."""
        all_rows = self.db.all_sessions()
        out = []
        for r in all_rows:
            ns_host = r["host"]
            pid, host = self._unns_host(ns_host)
            if provider and pid != provider:
                continue
            full = self.get(host, pid)
            if full:
                out.append(full)
        return out

    def list_by_provider(self) -> dict[str, list[SessionRecord]]:
        """Group sessions by provider."""
        grouped: dict[str, list[SessionRecord]] = {p: [] for p in ProviderId.all_named()}
        grouped[ProviderId.UNKNOWN] = []
        for rec in self.list_sessions():
            grouped.setdefault(rec.provider, []).append(rec)
        return grouped

    def record_success(self, host: str, provider: str) -> None:
        ns = self._ns_host(provider, host)
        self.db.record_success(ns)

    def record_challenge(self, host: str, provider: str) -> None:
        ns = self._ns_host(provider, host)
        self.db.record_challenge(ns)

    # ── info / stats ───────────────────────────────────
    def info(self, host: str, provider: str) -> dict:
        """Quick info without full record."""
        rec = self.ensure(host, provider)
        if not rec:
            return {"host": host, "provider": provider, "valid": False, "exists": False}
        return {
            "host": host,
            "provider": provider,
            "valid": rec.is_valid(),
            "exists": True,
            "has_cf_clearance": bool(rec.cf_clearance),
            "has_cf_bm": bool(rec.cf_bm),
            "expires_in": max(0, rec.cf_clearance_expires - int(time.time())),
            "cookie_count": len(rec.cookies),
            "challenge_count": rec.challenge_count,
            "success_count": rec.success_count,
        }

    # ── AWS WAF token application (NEW for v3) ──────────
    def apply_aws_waf_token(
        self, host: str, token_value: str, expires_in: int,
        source: str = "manual", notes: str = "",
    ) -> SessionRecord:
        """Write the AWS WAF token into the session as a cookie + extras metadata.

        Does NOT solve anything — just records the token. The token must be
        obtained out-of-band (browser, operator, or legitimate provider endpoint).
        """
        ns = self._ns_host(ProviderId.AWS_WAF, host)
        cookies = [
            {"name": "aws-waf-token", "value": token_value,
             "domain": host, "path": "/"},
        ]
        # merge with existing cookies (don't blow them away)
        rec = self.get(host, ProviderId.AWS_WAF)
        existing = list(rec.cookies) if rec else []
        existing = [c for c in existing if c.get("name") != "aws-waf-token"]
        existing.extend(cookies)
        # write to extras
        expires_at = int(time.time()) + expires_in if expires_in > 0 else 0
        self._extras[ns] = {
            "aws_waf_token": token_value,
            "aws_waf_expires_at": expires_at,
            "aws_waf_source": source,
            "aws_waf_notes": notes,
            "applied_at": int(time.time()),
        }
        self._save_extras()
        # save a minimal session (no cf_clearance, just cookies)
        return self.save(
            host=host, provider=ProviderId.AWS_WAF,
            user_agent="", fingerprint={},
            cf_clearance=None, cf_bm=None,
            cookies=existing,
            extra=self._extras[ns],
            cf_clearance_ttl=0,
        )

    def has_aws_waf_token(self, host: str) -> bool:
        """True if a non-expired AWS WAF token is stored for this host."""
        ns = self._ns_host(ProviderId.AWS_WAF, host)
        ex = self._extras.get(ns, {})
        if not ex.get("aws_waf_token"):
            return False
        expires_at = ex.get("aws_waf_expires_at", 0)
        return bool(expires_at and expires_at > int(time.time()))

    def get_aws_waf_token(self, host: str) -> dict | None:
        """Return AWS WAF token metadata dict or None if missing/expired.

        Shape: {value, expires_at, source, notes, applied_at, host}
        """
        ns = self._ns_host(ProviderId.AWS_WAF, host)
        ex = self._extras.get(ns, {})
        if not ex.get("aws_waf_token"):
            return None
        expires_at = ex.get("aws_waf_expires_at", 0)
        if expires_at and expires_at <= int(time.time()):
            return None
        return {
            "value": ex.get("aws_waf_token"),
            "expires_at": expires_at,
            "source": ex.get("aws_waf_source", "unknown"),
            "notes": ex.get("aws_waf_notes", ""),
            "applied_at": ex.get("applied_at", 0),
            "host": host,
            "expires_in": max(0, expires_at - int(time.time())),
        }

    def invalidate_aws_waf_token(self, host: str, reason: str = "manual") -> bool:
        """Remove the AWS WAF token from this session. Returns True if removed.

        Does NOT touch the cf_persistent.SessionDB row (the session itself
        may have other cookies). Just clears the token from extras and
        the cookies list.
        """
        ns = self._ns_host(ProviderId.AWS_WAF, host)
        ex = self._extras.get(ns, {})
        if not ex.get("aws_waf_token"):
            return False
        ex["aws_waf_invalidated"] = True
        ex["aws_waf_invalid_reason"] = reason
        ex["aws_waf_invalidated_at"] = int(time.time())
        # clear the cookie value but keep metadata for audit
        ex["aws_waf_token"] = ""
        self._save_extras()
        rec = self.get(host, ProviderId.AWS_WAF)
        if rec:
            new_cookies = [c for c in rec.cookies if c.get("name") != "aws-waf-token"]
            self.db.save_cookies(ns, new_cookies)
        return True

    def refresh_aws_waf_token(self, host: str) -> bool:
        """Convenience: invalidate with reason='rotate' (forces fresh acquire)."""
        return self.invalidate_aws_waf_token(host, reason="rotate")

    def stats(self) -> dict:
        """Aggregate stats across all sessions."""
        all_recs = self.list_sessions()
        by_provider: dict[str, int] = {}
        valid_count = 0
        for r in all_recs:
            by_provider[r.provider] = by_provider.get(r.provider, 0) + 1
            if r.is_valid():
                valid_count += 1
        return {
            "total": len(all_recs),
            "valid": valid_count,
            "by_provider": by_provider,
        }

    # ── refresh (calls into cf_selenium for CF) ────────
    def refresh_session(
        self, url: str, host: str, provider: str, browser_factory=None,
    ) -> SessionRecord:
        """Re-solve the challenge and save a fresh session.

        For non-CF providers, browser_factory should be the adapter's
        solve function (raises if not implemented).

        browser_factory: callable(url) -> (cookies, user_agent, fingerprint) | raise
        """
        if browser_factory is None:
            if provider == ProviderId.CLOUDFLARE:
                browser_factory = self._cf_default_factory
            else:
                raise NotImplementedError(
                    f"no default browser_factory for provider={provider!r}; "
                    f"pass an adapter-specific solver"
                )
        cookies, ua, fp, extra = browser_factory(url)
        ttl = 1800
        if isinstance(extra, dict) and "ttl" in extra:
            ttl = int(extra["ttl"])
        cf_clearance = None
        cf_bm = None
        if isinstance(extra, dict):
            cf_clearance = extra.get("cf_clearance")
            cf_bm = extra.get("cf_bm")
        return self.save(
            host=host, provider=provider,
            user_agent=ua, fingerprint=fp,
            cf_clearance=cf_clearance, cf_bm=cf_bm,
            cookies=cookies or [],
            extra=extra if isinstance(extra, dict) else {},
            cf_clearance_ttl=ttl,
        )

    def _cf_default_factory(self, url: str) -> tuple[list, str, dict, dict]:
        """Default factory for Cloudflare: delegates to cf_selenium.Browser."""
        from cf_selenium import Browser
        b = Browser(profile=f"bypass_{int(time.time())}", auto_solve=True)
        try:
            b.get(url, wait_for="body")
            cookies = []
            for c in b.cookies.items() if isinstance(b.cookies, dict) else []:
                cookies.append({"name": c[0], "value": c[1]})
            ua = b.execute_script("return navigator.userAgent") or ""
            fp = {}
            return (cookies, ua, fp, {"ttl": 1800, "cf_clearance": b.cookies.get("cf_clearance") if isinstance(b.cookies, dict) else None})
        finally:
            b.quit()


# ── CLI / smoke test ─────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== BypassSession smoke test ===")
    bs = BypassSession(db_path=HOME / ".cf_agent" / "sessions.db")

    # save test
    rec = bs.save(
        host="127.0.0.1", provider=ProviderId.CLOUDFLARE,
        user_agent="test-ua", fingerprint={"x": 1},
        cf_clearance="abc123", cf_bm=None, cookies=[{"name": "a", "value": "b"}],
        extra={"foo": "bar"}, cf_clearance_ttl=3600,
    )
    print(f"saved: provider={rec.provider} host={rec.host} valid={rec.is_valid()} extra={rec.extra}")

    # get
    got = bs.get("127.0.0.1", ProviderId.CLOUDFLARE)
    print(f"got: clearance={got.cf_clearance[:10] if got.cf_clearance else None} cookies={len(got.cookies)}")

    # ensure
    ens = bs.ensure("127.0.0.1", ProviderId.CLOUDFLARE)
    print(f"ensure: {ens is not None}")

    # info
    print(f"info: {bs.info('127.0.0.1', ProviderId.CLOUDFLARE)}")

    # list
    print(f"list (all): {[(r.provider, r.host) for r in bs.list_sessions()]}")
    print(f"list (cloudflare): {[(r.provider, r.host) for r in bs.list_sessions(ProviderId.CLOUDFLARE)]}")

    # list_by_provider
    print(f"by_provider: {{p: len(recs) for p, recs in bs.list_by_provider().items()}}")

    # stats
    print(f"stats: {bs.stats()}")

    # save to different provider same host (test namespacing)
    rec2 = bs.save(
        host="127.0.0.1", provider=ProviderId.AKAMAI,
        user_agent="test-ua", fingerprint={"x": 2},
        extra={"sensor": "fake"}, cf_clearance_ttl=1800,
    )
    print(f"akamai same host: valid={rec2.is_valid()} extra={rec2.extra}")

    # list_by_provider should show 2 records
    grouped = bs.list_by_provider()
    print(f"after akamai: cloudflare={len(grouped[ProviderId.CLOUDFLARE])} akamai={len(grouped[ProviderId.AKAMAI])}")

    # cleanup
    bs.delete("127.0.0.1", ProviderId.CLOUDFLARE)
    bs.delete("127.0.0.1", ProviderId.AKAMAI)
    print("cleanup done")
