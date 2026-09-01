"""specter — staging-only AI web-agent with multi-provider protection classification,
token-lifecycle management, and vision-based decision support.

Use when:
  - You need an AI web-agent that inspects pages and acts on them
  - You want to *classify* (not solve) anti-bot protections across vendors
  - You manage AWS WAF / Cloudflare / Akamai / DataDome / Imperva / reCAPTCHA /
    hCaptcha / Arkose tokens in a structured, auditable way
  - You want a decision layer that combines DOM signals + OCR (Tesseract) +
    optional Claude Vision, with explicit confidence thresholds

Out of scope:
  - Auto-solving JS challenges
  - Fabricating tokens or session cookies
  - Attacking unauthorized targets
"""

__version__ = "0.5.0"
__author__ = "letticha"

from specter.agent import AIWebAgent, TraceEvent, AgentResult
from specter.config import get_config
from specter.sessions import BypassSession

__all__ = ["AIWebAgent", "TraceEvent", "AgentResult", "BypassSession", "get_config", "__version__"]
