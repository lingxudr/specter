"""Vision abstraction for cf_agent.

Provides multiple backends for extracting semantic info from screenshots:
- Tesseract OCR (local, no API key, works in proot)
- Claude Vision API (cloud, requires key, higher quality)
- Local CV (DOM-based via CDP, no image processing needed)

Usage:
    from specter.vision import get_vision
    v = get_vision()  # auto-picks best available provider
    result = v.analyze("challenge.png")
    if "verify" in result.text.lower():
        ...
"""
from __future__ import annotations

import enum
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class VisionTask(str, enum.Enum):
    """What kind of analysis to perform on the image."""
    OCR = "ocr"                  # just extract text
    CLASSIFY = "classify"        # what is this? (challenge/captcha/login/etc)
    CHALLENGE = "challenge"      # solve a visual challenge (e.g. "click all X")
    ELEMENT_LOCATE = "locate"    # find coords of element matching description


@dataclass
class VisionResult:
    """Result from a vision provider."""
    text: str = ""                                    # OCR text
    label: str = ""                                   # classification label
    confidence: float = 0.0                           # 0..1
    elements: list[dict] = field(default_factory=list)  # found items: {label, bbox, confidence}
    provider: str = ""                                # which provider produced this
    raw: dict = field(default_factory=dict)            # provider-specific extras
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:
        bits = [f"provider={self.provider}"]
        if self.text:
            bits.append(f"text={self.text[:60]!r}{'...' if len(self.text) > 60 else ''}")
        if self.label:
            bits.append(f"label={self.label}")
        if self.confidence:
            bits.append(f"conf={self.confidence:.2f}")
        if self.error:
            bits.append(f"error={self.error}")
        return "VisionResult(" + " ".join(bits) + ")"


class VisionProvider(ABC):
    """Abstract base class for vision backends."""
    name: str = "base"
    available: bool = False

    @abstractmethod
    def analyze(self, image_path: str, task: VisionTask = VisionTask.OCR,
                prompt: str = "", timeout: float = 30.0) -> VisionResult:
        """Analyze an image file and return structured info."""
        raise NotImplementedError

    def is_ready(self) -> tuple[bool, str]:
        """Return (ready, reason). Override to add provider-specific checks."""
        return self.available, f"provider {self.name!r} availability={self.available}"


# ---------------- Tesseract provider ----------------

class TesseractProvider(VisionProvider):
    """Local OCR via tesseract binary. Fast, no API, works offline."""

    name = "tesseract"
    DEFAULT_PSM = 6  # uniform block of text — best for captcha-style content

    def __init__(self, tesseract_cmd: str = "tesseract"):
        self.cmd = tesseract_cmd
        self.available = shutil.which(tesseract_cmd) is not None
        self.version = self._detect_version() if self.available else ""

    def _detect_version(self) -> str:
        try:
            r = subprocess.run(
                [self.cmd, "--version"], capture_output=True, text=True, timeout=5
            )
            return r.stdout.split("\n", 1)[0] if r.stdout else ""
        except Exception:
            return ""

    def is_ready(self) -> tuple[bool, str]:
        if not self.available:
            return False, f"tesseract binary not found at {self.cmd!r}"
        return True, f"tesseract ready: {self.version}"

    def analyze(self, image_path: str, task: VisionTask = VisionTask.OCR,
                prompt: str = "", timeout: float = 30.0) -> VisionResult:
        if not self.available:
            return VisionResult(
                provider=self.name, error="tesseract not available"
            )
        if not os.path.exists(image_path):
            return VisionResult(
                provider=self.name, error=f"image not found: {image_path}"
            )
        if task not in (VisionTask.OCR, VisionTask.CLASSIFY):
            return VisionResult(
                provider=self.name,
                error=f"tesseract only supports OCR/CLASSIFY, got {task.value}",
            )

        psm = self.DEFAULT_PSM
        # Different tasks may want different PSM
        if task == VisionTask.OCR:
            psm = 6  # single uniform block
        elif task == VisionTask.CLASSIFY:
            psm = 11  # sparse text — find any text anywhere

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp_base = tmp.name[:-4]
        try:
            r = subprocess.run(
                [self.cmd, image_path, tmp_base, "--psm", str(psm)],
                capture_output=True, text=True, timeout=timeout,
            )
            text = ""
            if os.path.exists(tmp_base + ".txt"):
                with open(tmp_base + ".txt", "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read().strip()
            if r.returncode != 0 and not text:
                return VisionResult(
                    provider=self.name,
                    error=f"tesseract exit {r.returncode}: {r.stderr[:200]}",
                )
            return VisionResult(
                text=text,
                confidence=0.8 if text else 0.0,  # tesseract doesn't always return conf per call
                provider=self.name,
                raw={"stdout": r.stdout, "stderr": r.stderr, "psm": psm},
            )
        except subprocess.TimeoutExpired:
            return VisionResult(
                provider=self.name,
                error=f"tesseract timeout after {timeout}s",
            )
        except Exception as e:
            return VisionResult(
                provider=self.name, error=f"tesseract error: {e}"
            )
        finally:
            for ext in (".txt",):
                p = tmp_base + ext
                if os.path.exists(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass


# ---------------- Claude Vision provider (stub, opt-in) ----------------

class ClaudeVisionProvider(VisionProvider):
    """Claude Vision API. Requires ANTHROPIC_API_KEY env var. Cloud, costs $$."""

    name = "claude_vision"
    DEFAULT_MODEL = "claude-sonnet-4-5"
    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or self.DEFAULT_MODEL
        self.available = bool(self.api_key)
        self._client = None

    def is_ready(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY not set"
        # Don't actually init httpx here — that would require the dep
        return True, f"claude vision ready (model={self.model})"

    def analyze(self, image_path: str, task: VisionTask = VisionTask.OCR,
                prompt: str = "", timeout: float = 60.0) -> VisionResult:
        if not self.available:
            return VisionResult(
                provider=self.name, error="ANTHROPIC_API_KEY not set"
            )
        if not os.path.exists(image_path):
            return VisionResult(
                provider=self.name, error=f"image not found: {image_path}"
            )

        # Lazy import — only fail if user actually tries to use this
        try:
            import httpx  # noqa
        except ImportError:
            return VisionResult(
                provider=self.name,
                error="httpx not installed (pip install httpx)",
            )

        # Lazy import for base64
        import base64
        with open(image_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("ascii")

        # Build the prompt by task
        if task == VisionTask.OCR:
            default_prompt = "Transcribe all text in this image exactly as it appears. Output only the text, no commentary."
        elif task == VisionTask.CLASSIFY:
            default_prompt = "What type of screen is this? Answer with one of: cloudflare_challenge, recaptcha, hcaptcha, login_form, error_page, normal_page. Output only the label."
        elif task == VisionTask.CHALLENGE:
            default_prompt = prompt or "Describe the challenge and what action is required to solve it."
        elif task == VisionTask.ELEMENT_LOCATE:
            default_prompt = prompt or "Locate the element described. Return bounding box as {x,y,w,h} in pixels."
        else:
            default_prompt = prompt or "Describe what you see in this image."

        # Get mime type
        ext = Path(image_path).suffix.lower().lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/png")

        body = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": image_data,
                    }},
                    {"type": "text", "text": default_prompt},
                ],
            }],
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(
                    self.API_URL,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=body,
                )
            if r.status_code != 200:
                return VisionResult(
                    provider=self.name,
                    error=f"claude API {r.status_code}: {r.text[:200]}",
                )
            data = r.json()
            content = data.get("content", [])
            text = ""
            for block in content:
                if block.get("type") == "text":
                    text += block.get("text", "")
            label = ""
            if task == VisionTask.CLASSIFY:
                label = text.strip().split()[0].lower() if text else ""
            return VisionResult(
                text=text.strip(),
                label=label,
                confidence=0.95,
                provider=self.name,
                raw={"model": self.model, "usage": data.get("usage", {})},
            )
        except Exception as e:
            return VisionResult(
                provider=self.name, error=f"claude vision error: {e}"
            )


# ---------------- DOM-based "vision" (no image needed) ----------------

class DomProvider(VisionProvider):
    """Uses cf_selenium's accessibility tree instead of pixels.

    Cheaper, faster, no OCR needed. Works when we have a live browser tab.
    """

    name = "dom"

    def __init__(self, browser=None):
        # browser is cf_selenium.Browser — passed at analyze time
        self.available = True  # always available, requires browser arg at analyze

    def analyze(self, image_path: str, task: VisionTask = VisionTask.OCR,
                prompt: str = "", timeout: float = 30.0) -> VisionResult:
        return VisionResult(
            provider=self.name,
            error="DomProvider requires browser; use browser.dom_snapshot() directly",
        )

    def analyze_snapshot(self, snap: dict, task: VisionTask = VisionTask.OCR) -> VisionResult:
        """Direct analysis of a cf_selenium accessibility snapshot.

        snap structure: {title, url, accessibility: [{role, name, ...}, ...]}
        """
        if not snap:
            return VisionResult(provider=self.name, error="empty snapshot")

        text_parts = []
        elements = []
        for node in snap.get("accessibility", []):
            name = node.get("name", "").strip()
            role = node.get("role", "").strip()
            if name:
                text_parts.append(f"[{role}] {name}")
            if role in ("button", "link", "textbox", "checkbox", "img"):
                elements.append({
                    "label": name or role,
                    "role": role,
                    "nodeId": node.get("nodeId"),
                })

        text = "\n".join(text_parts)
        label = ""
        if task == VisionTask.CLASSIFY:
            label = self._classify(snap, text)

        return VisionResult(
            text=text,
            label=label,
            confidence=0.7,
            provider=self.name,
            elements=elements,
            raw={"url": snap.get("url", ""), "title": snap.get("title", "")},
        )

    def _classify(self, snap: dict, text: str) -> str:
        t = text.lower()
        u = snap.get("url", "").lower()
        # Tag/class signals — extract from MULTIPLE possible shapes since
        # cf_selenium.snapshot() doesn't return raw accessibility tree.
        # Shape 1: raw CDP accessibility list (full class info)
        ax = snap.get("accessibility", []) or []
        # Shape 2: cf_selenium snapshot (headings/buttons/links with class+id)
        class_signals = []
        for n in ax:
            cls = n.get("class", "") or ""
            if cls:
                class_signals.append(cls)
        for h in snap.get("headings", []) or []:
            if isinstance(h, dict):
                class_signals.append(h.get("class", "") or "")
        for b in snap.get("buttons", []) or []:
            if isinstance(b, dict):
                class_signals.append(b.get("class", "") or "")
        # Shape 3: HTML body attributes (when include_html=True).
        # Real-world challenges vary — extract class= AND name= AND id= AND src=.
        # (cf_selenium.snapshot() only exposes these via raw HTML.)
        import re as _re
        html = snap.get("html", "") or ""
        class_signals = class_signals  # preserve shape 1+2 contributions
        for attr in ("class", "name", "id", "src", "data-sitekey"):
            for m in _re.finditer(rf'{attr}="([^"]+)"', html):
                class_signals.append(m.group(1))
        tags_blob = " ".join(class_signals).lower() + " " + t
        # Cloudflare signals
        if (
            "just a moment" in t
            or "cf_chl_opt" in u
            or "checking your browser" in t
            or "cf-challenge" in tags_blob
            or "cf-turnstile" in tags_blob
            or "cf-spinner" in tags_blob
        ):
            return "cloudflare_challenge"
        if (
            "recaptcha" in t
            or "i'm not a robot" in t
            or "g-recaptcha" in tags_blob
        ):
            return "recaptcha"
        if "hcaptcha" in t or "h-captcha" in tags_blob:
            return "hcaptcha"
        if "verify you are human" in t or "cf-turnstile" in tags_blob:
            return "turnstile"
        if ("awswaf" in t or "aws-waf" in t or "aws-waf-token" in tags_blob
                or "challenge.js" in t):
            return "aws_waf"
        if "akamai" in t or "_abck" in tags_blob:
            return "akamai_challenge"
        if "perimeterx" in t or "_pxhd" in tags_blob:
            return "perimeterx_challenge"
        if (
            "sign in" in t
            or "log in" in t
            or "password" in t
            or "login" in tags_blob
        ):
            return "login_form"
        if "404" in t or "not found" in t:
            return "error_page"
        if "arkose" in t or "funcaptcha" in t:
            return "arkose"
        return "normal_page"


# ---------------- Registry / dispatcher ----------------

_PROVIDERS: dict[str, VisionProvider] = {}


def register_provider(provider: VisionProvider) -> None:
    """Register a vision provider instance."""
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> Optional[VisionProvider]:
    return _PROVIDERS.get(name)


def list_providers() -> list[dict]:
    """List all registered providers with their ready status."""
    out = []
    for p in _PROVIDERS.values():
        ready, reason = p.is_ready()
        out.append({
            "name": p.name,
            "ready": ready,
            "reason": reason,
        })
    return out


def get_vision(name: Optional[str] = None) -> VisionProvider:
    """Get a vision provider.

    If name is given, return that specific provider.
    Otherwise auto-pick the best available one by priority:
      claude_vision > tesseract > dom
    """
    if not _PROVIDERS:
        register_provider(TesseractProvider())
        register_provider(ClaudeVisionProvider())
        register_provider(DomProvider())

    if name:
        p = _PROVIDERS.get(name)
        if p is None:
            raise KeyError(f"unknown vision provider: {name!r}. "
                           f"available: {list(_PROVIDERS)}")
        return p

    # Auto-pick: claude > tesseract > dom
    for pref in ("claude_vision", "tesseract", "dom"):
        p = _PROVIDERS.get(pref)
        if p and p.is_ready()[0]:
            return p
    # Fallback to first registered
    return next(iter(_PROVIDERS.values()))


# Pre-register defaults on import
register_provider(TesseractProvider())
register_provider(ClaudeVisionProvider())
register_provider(DomProvider())


def analyze_page(
    snapshot: dict | None = None,
    png_path: str | Path | None = None,
    host: str = "",
    *,
    use_tesseract: bool = True,
    use_claude: bool = True,
    use_dom: bool = True,
) -> dict[str, VisionResult]:
    """Run all enabled vision providers and return ALL candidates as a dict.

    Unlike classify_page() (which returns the single best result), this returns
    every provider's VisionResult so callers can merge evidence from multiple
    sources (e.g. DOM says "cloudflare" but Tesseract says "captcha challenge").

    Returns:
        dict with keys: "dom", "tesseract", "claude" (missing if not requested/errored).
    """
    snapshot = snapshot or {}
    out: dict[str, VisionResult] = {}

    if use_dom:
        dom = get_provider("dom")
        if dom:
            try:
                # DomProvider has analyze_snapshot; base class only has analyze(image_path,...)
                if "analyze_snapshot" in type(dom).__dict__:
                    r = dom.analyze_snapshot(snapshot, task=VisionTask.CLASSIFY)
                else:
                    r = dom.analyze(snapshot, task=VisionTask.CLASSIFY)  # type: ignore[arg-type]
            except Exception as e:  # noqa: BLE001
                r = VisionResult(provider="dom", error=str(e))
            out["dom"] = r

    if use_tesseract and png_path:
        tess = get_provider("tesseract")
        if tess:
            try:
                r = tess.analyze(str(png_path), task=VisionTask.OCR)
            except Exception as e:  # noqa: BLE001
                r = VisionResult(provider="tesseract", error=str(e))
            out["tesseract"] = r

    if use_claude and png_path:
        claude = get_provider("claude_vision")
        if claude:
            try:
                # No SPATIAL task — use CHALLENGE which fits Claude's role
                r = claude.analyze(str(png_path), task=VisionTask.CHALLENGE)
            except Exception as e:  # noqa: BLE001
                r = VisionResult(provider="claude_vision", error=str(e))
            out["claude"] = r

    return out


def classify_page(
    snapshot: dict | None = None,
    png_path: str | Path | None = None,
    host: str = "",
    *,
    use_tesseract: bool = True,
    use_claude: bool = True,
    use_dom: bool = True,
    min_confidence: float = 0.5,
) -> "VisionResult":
    """Run all enabled vision providers and merge their results.

    Tries each provider in priority order. Returns the first result with
    confidence >= min_confidence. If none reaches the threshold, returns
    the highest-confidence result.

    Args:
        snapshot: page snapshot dict (from cf_selenium.snapshot())
        png_path: path to screenshot (for OCR/Claude)
        host: hostname for context
        use_*: toggle individual providers
        min_confidence: minimum confidence to accept early

    Returns:
        Best VisionResult.
    """
    snapshot = snapshot or {}
    candidates: list[VisionResult] = []

    if use_dom:
        dom = get_provider("dom")
        if dom:
            try:
                r = dom.analyze(snapshot, host=host)
                candidates.append(r)
            except Exception as e:  # noqa: BLE001
                candidates.append(VisionResult(
                    provider="dom", error=str(e),
                ))

    if use_tesseract and png_path:
        tess = get_provider("tesseract")
        if tess:
            try:
                r = tess.analyze(snapshot, png_path=png_path, host=host)
                candidates.append(r)
            except Exception as e:  # noqa: BLE001
                candidates.append(VisionResult(
                    provider="tesseract", error=str(e),
                ))

    if use_claude and png_path:
        claude = get_provider("claude_vision")
        if claude:
            try:
                r = claude.analyze(snapshot, png_path=png_path, host=host)
                candidates.append(r)
            except Exception as e:  # noqa: BLE001
                candidates.append(VisionResult(
                    provider="claude_vision", error=str(e),
                ))

    if not candidates:
        return VisionResult(
            provider="none", error="no providers enabled",
        )

    # Early-return first provider above threshold (priority order: dom, tesseract, claude)
    for c in candidates:
        if c.ok and c.confidence >= min_confidence:
            return c
    # Otherwise best confidence wins (skip errored)
    ok_cands = [c for c in candidates if c.ok]
    if not ok_cands:
        return candidates[0]  # return first (with error)
    return max(ok_cands, key=lambda c: c.confidence)


__all__ = [
    "VisionTask", "VisionResult", "VisionProvider",
    "TesseractProvider", "ClaudeVisionProvider", "DomProvider",
    "register_provider", "get_provider", "get_vision", "list_providers",
]
