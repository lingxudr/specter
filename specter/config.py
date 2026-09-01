#!/usr/bin/env python3
"""
cf_agent_config.py — Config loader for cf_agent.

Priority: env var > YAML/JSON file > defaults.

Defaults: bypass DISABLED. User must explicitly enable via env or file.
Reasoning layer is ALWAYS bypass-off (no solver, no stealth inject).
Only the browser/scraping tool layer reads the bypass flag.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── Defaults ──────────────────────────────────────────────────────────
DEFAULTS: dict[str, Any] = {
    "bypass": {
        "enabled": False,
        "authorized_test_mode": True,
        "allowed_domains": [],
        "challenge_strategy": "auto_solve",
        "session_storage": "~/.cf_agent/sessions.db",
        "confidence_threshold": 0.6,
        "cache_ttl_seconds": 300,
    },
    "llm": {
        "enabled": True,
        "model": "Free-All",
        "router_url": "http://127.0.0.1:20130/v1",
    },
    "agent": {
        "max_steps": 10,
        "headless": True,
        "wait_per_step": 0.5,
        "screenshot_before_after": True,
    },
    "logging": {
        "level": "INFO",
        "log_file": "~/.cf_agent/logs/agent.log",
    },
}


# ── Data classes ──────────────────────────────────────────────────────
@dataclass
class BypassConfig:
    """Main config. Loaded once at agent start; immutable after load."""
    bypass: dict = field(default_factory=lambda: dict(DEFAULTS["bypass"]))
    llm: dict = field(default_factory=lambda: dict(DEFAULTS["llm"]))
    agent: dict = field(default_factory=lambda: dict(DEFAULTS["agent"]))
    logging: dict = field(default_factory=lambda: dict(DEFAULTS["logging"]))
    config_source: str = "default"  # "env" | "file" | "default"

    # ── convenience accessors ─────────────────────────────
    @property
    def bypass_enabled(self) -> bool:
        return bool(self.bypass.get("enabled", False))

    @property
    def authorized_test_mode(self) -> bool:
        return bool(self.bypass.get("authorized_test_mode", True))

    @property
    def allowed_domains(self) -> list[str]:
        return list(self.bypass.get("allowed_domains", []))

    @property
    def challenge_strategy(self) -> str:
        return str(self.bypass.get("challenge_strategy", "auto_solve"))

    @property
    def confidence_threshold(self) -> float:
        return float(self.bypass.get("confidence_threshold", 0.6))

    @property
    def cache_ttl_seconds(self) -> int:
        return int(self.bypass.get("cache_ttl_seconds", 300))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm.get("enabled", True))

    @property
    def llm_model(self) -> str:
        return str(self.llm.get("model", "Free-All"))

    @property
    def llm_router_url(self) -> str:
        return str(self.llm.get("router_url", "http://127.0.0.1:20130/v1"))

    @property
    def max_steps(self) -> int:
        return int(self.agent.get("max_steps", 10))

    @property
    def headless(self) -> bool:
        return bool(self.agent.get("headless", True))

    # ── authorization check ───────────────────────────────
    def is_domain_allowed(self, host: str) -> bool:
        for ok in self.allowed_domains:
            if host == ok or host.endswith("." + ok):
                return True
        return False

    def check_authorization(self, url: str) -> tuple[bool, str]:
        """Verify URL is allowed + mode is authorized_test."""
        from urllib.parse import urlparse
        if not self.authorized_test_mode:
            return False, f"refused: authorized_test_mode=False"
        if not self.bypass_enabled:
            return False, f"refused: bypass.enabled=False (env CF_BYPASS_ENABLED or config file)"
        if not self.allowed_domains:
            return False, "refused: allowed_domains is empty (set bypass.allowed_domains in config)"
        try:
            parsed = urlparse(url)
        except Exception as e:
            return False, f"refused: invalid URL {url!r}: {e}"
        host = parsed.hostname or ""
        if not self.is_domain_allowed(host):
            return False, f"refused: {host} not in allowed_domains {self.allowed_domains}"
        return True, f"ok: {host} allowed"

    # ── serialization ─────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "bypass": dict(self.bypass),
            "llm": dict(self.llm),
            "agent": dict(self.agent),
            "logging": dict(self.logging),
            "config_source": self.config_source,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ── ConfigLoader ──────────────────────────────────────────────────────
class ConfigLoader:
    """Load config from env vars (highest), YAML/JSON file, defaults (lowest)."""

    ENV_BYPASS_ENABLED = "CF_BYPASS_ENABLED"
    ENV_AUTHORIZED = "CF_AUTHORIZED_TEST_MODE"
    ENV_LLM_MODEL = "CF_AGENT_MODEL"
    ENV_MAX_STEPS = "CF_AGENT_MAX_STEPS"
    ENV_CONFIG_PATH = "CF_AGENT_CONFIG"

    @classmethod
    def from_defaults(cls) -> BypassConfig:
        return BypassConfig(config_source="default")

    @classmethod
    def from_file(cls, path: str | Path) -> BypassConfig:
        path = Path(os.path.expanduser(str(path)))
        if not path.exists():
            raise FileNotFoundError(f"config not found: {path}")
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            if not _HAS_YAML:
                raise RuntimeError("yaml not available; install pyyaml or use .json")
            data = yaml.safe_load(text) or {}
        elif path.suffix == ".json":
            data = json.loads(text)
        else:
            raise ValueError(f"unsupported config extension: {path.suffix} (use .yaml or .json)")
        return cls._merge_with_defaults(data, source="file")

    @classmethod
    def from_env(cls, base: BypassConfig | None = None) -> BypassConfig:
        """Apply env-var overrides to base (defaults if None)."""
        cfg = base or cls.from_defaults()
        new_source = cfg.config_source

        # CF_BYPASS_ENABLED (most important)
        env = os.environ.get(cls.ENV_BYPASS_ENABLED)
        if env is not None:
            cfg.bypass["enabled"] = _parse_bool(env)
            new_source = "env"

        # CF_AUTHORIZED_TEST_MODE
        env = os.environ.get(cls.ENV_AUTHORIZED)
        if env is not None:
            cfg.bypass["authorized_test_mode"] = _parse_bool(env)
            new_source = "env"

        # CF_AGENT_MODEL
        env = os.environ.get(cls.ENV_LLM_MODEL)
        if env:
            cfg.llm["model"] = env
            new_source = "env"

        # CF_AGENT_MAX_STEPS
        env = os.environ.get(cls.ENV_MAX_STEPS)
        if env:
            try:
                cfg.agent["max_steps"] = int(env)
                new_source = "env"
            except ValueError:
                pass

        # CF_BYPASS_ALLOW (comma-separated domains, env convenience)
        env = os.environ.get("CF_BYPASS_ALLOW")
        if env:
            cfg.bypass["allowed_domains"] = [d.strip() for d in env.split(",") if d.strip()]
            new_source = "env"

        cfg.config_source = new_source if new_source != "default" else cfg.config_source
        return cfg

    @classmethod
    def load(cls, path: str | Path | None = None) -> BypassConfig:
        """Load config with priority: env > file > defaults.

        Order:
          1. defaults
          2. file (if path given or CF_AGENT_CONFIG env)
          3. env overrides
        """
        # resolve config file path
        if path is None:
            env = os.environ.get(cls.ENV_CONFIG_PATH)
            if env:
                path = env
            else:
                default_path = Path(os.path.expanduser("~/.cf_agent/config.yaml"))
                if default_path.exists():
                    path = default_path
                else:
                    json_path = Path(os.path.expanduser("~/.cf_agent/config.json"))
                    if json_path.exists():
                        path = json_path

        # start from defaults
        cfg = cls.from_defaults()

        # merge file if available
        if path is not None:
            try:
                cfg = cls.from_file(path)
            except FileNotFoundError:
                pass  # fall through to defaults
            except Exception as e:
                import sys
                print(f"warn: config file load failed: {e}", file=sys.stderr)

        # apply env overrides
        cfg = cls.from_env(cfg)

        # safety check: if bypass enabled but authorized_test_mode off, warn
        if cfg.bypass_enabled and not cfg.authorized_test_mode:
            import sys
            print(
                f"warn: bypass.enabled=True but authorized_test_mode=False; "
                f"this is a safety violation. Tools will be blocked.",
                file=sys.stderr,
            )

        return cfg

    @classmethod
    def _merge_with_defaults(cls, user_data: dict, source: str) -> BypassConfig:
        """Deep-merge user data with defaults."""
        merged = {
            "bypass": {**DEFAULTS["bypass"], **(user_data.get("bypass") or {})},
            "llm": {**DEFAULTS["llm"], **(user_data.get("llm") or {})},
            "agent": {**DEFAULTS["agent"], **(user_data.get("agent") or {})},
            "logging": {**DEFAULTS["logging"], **(user_data.get("logging") or {})},
        }
        return BypassConfig(**merged, config_source=source)


# ── Singleton ─────────────────────────────────────────────────────────
_singleton: BypassConfig | None = None


def get_config(reload: bool = False, path: str | Path | None = None) -> BypassConfig:
    """Get cached config singleton. Pass reload=True to re-read from env/file."""
    global _singleton
    if _singleton is None or reload or path is not None:
        _singleton = ConfigLoader.load(path)
    return _singleton


def reset_config() -> None:
    global _singleton
    _singleton = None


# ── Helpers ───────────────────────────────────────────────────────────
def _parse_bool(s: str) -> bool:
    s = s.strip().lower()
    if s in ("1", "true", "yes", "on", "enable", "enabled"):
        return True
    if s in ("0", "false", "no", "off", "disable", "disabled"):
        return False
    return bool(s)


# ── CLI sanity check ─────────────────────────────────────────────────
if __name__ == "__main__":
    cfg = get_config()
    print("=== loaded config ===")
    print(f"source: {cfg.config_source}")
    print(f"bypass.enabled: {cfg.bypass_enabled}")
    print(f"bypass.authorized_test_mode: {cfg.authorized_test_mode}")
    print(f"bypass.allowed_domains: {cfg.allowed_domains}")
    print(f"llm.model: {cfg.llm_model}")
    print(f"agent.max_steps: {cfg.max_steps}")
    print("\n=== JSON output ===")
    print(cfg.to_json())
