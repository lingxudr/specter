#!/usr/bin/env python3
"""
cf_agent.py — AI web-agent powered by cf_selenium.

Capabilities (per task spec):
  1. open a page
  2. run snapshot()
  3. understand title/headings/links/forms/buttons/inputs
  4. choose action based on user goal
  5. execute click/type/navigate
  6. snapshot again after each action
  7. detect page changes
  8. (authorized_test_mode) detect challenge, run solver, verify, continue
  9. execution trace with action/target/URL/timestamp/result
 10. screenshot before and after each action

Inputs (hybrid):
  - CLI args:  python3 cf_agent.py <url> <goal> [--max-steps N] [--profile P]
  - Task JSON: python3 cf_agent.py --task task.json
        task.json = {
          "url": "https://staging.example.com",
          "goal": "Find pricing and report first plan",
          "max_steps": 10,
          "profile": "default",
          "browser_headless": true,
          "verify_ssl": true
        }
  - Python:    from specter.agent import AIWebAgent
               with AIWebAgent(profile="x") as agent:
                   result = agent.run("https://staging.example.com",
                                      "click the first product")

Outputs (both):
  - File:     ~/.cf_agent/runs/<run_id>/
        - trace.jsonl      (one event per action)
        - trace.json       (summary)
        - screenshots/     (before_*.png, after_*.png)
        - result.json      (final state)
  - Return:   AgentResult dataclass (steps, success, final_url, final_title, summary)

Safety:
  - allowed_domains list (default empty = requires explicit CLI list)
  - authorized_test_mode flag (default true) gates any non-trivial decision
  - CF bypass only runs in authorized_test_mode + on allowed_domains

Author: letticha
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# add home so we can import cf_selenium
HOME = Path(os.path.expanduser("~"))
sys.path.insert(0, str(HOME))

try:
    from cf_selenium import Browser
except ImportError as e:
    print(f"FATAL: cf_selenium import failed: {e}", file=sys.stderr)
    raise

# ── Config ─────────────────────────────────────────────────────────────
AGENT_DIR = HOME / ".cf_agent"
RUNS_DIR = AGENT_DIR / "runs"
LOGS_DIR = AGENT_DIR / "logs"
for d in (AGENT_DIR, RUNS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# allowed domains = list; empty list = ALL denied (must set explicitly)
ALLOWED_DOMAINS_DEFAULT: list[str] = [
    # user-controlled staging examples (edit per project):
    # "staging.example.com",
    # "localhost",
    # "127.0.0.1",
]

# Routerku for hybrid AI decision
ROUTER_URL = os.environ.get("CF_AGENT_ROUTER_URL", "http://127.0.0.1:20130/v1")
ROUTER_KEY = os.environ.get(
    "HERMES_CUSTOM_127_0_0_1_20130_API_KEY",
    os.environ.get("HERMES_CUSTOM_LOCALHOST_20130_API_KEY", ""),
)
ROUTER_MODEL = os.environ.get("CF_AGENT_MODEL", "Free-All")
ROUTER_TIMEOUT = 30  # seconds

# ── Logging ────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def log(level: str, msg: str) -> None:
    print(f"{_ts()} [{level.upper():5}] {msg}", file=sys.stderr, flush=True)


# ── Data classes ───────────────────────────────────────────────────────
@dataclass
class TraceEvent:
    step: int
    action: str
    target: str
    url: str
    timestamp: str
    result: str
    duration_ms: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    success: bool
    run_id: str
    start_url: str
    final_url: str
    final_title: str
    goal: str
    steps: list[TraceEvent]
    summary: str
    error: str | None = None
    artifacts_dir: str = ""


# ── Page signature for change detection ────────────────────────────────
def page_signature(snap: dict) -> str:
    """Stable hash of (url, title, heading-count, link-count, action-count, cookie-count)."""
    h = hashlib.sha256()
    payload = json.dumps({
        "url": snap.get("url", ""),
        "title": snap.get("title", ""),
        "h_n": len(snap.get("headings", [])),
        "l_n": len(snap.get("links", [])),
        "a_n": len(snap.get("actions", [])),
        "c_n": snap.get("cookies_count", 0),
        "ch": snap.get("challenge", "none"),
    }, sort_keys=True)
    h.update(payload.encode())
    return h.hexdigest()[:16]


# ── LLM client (routerku) ─────────────────────────────────────────────
def call_llm(prompt: str, system: str = "", model: str | None = None) -> str | None:
    """Call routerku for LLM-based action decision. Returns None on failure."""
    if not ROUTER_KEY:
        log("warn", "LLM disabled: no API key in env")
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps({
        "model": model or ROUTER_MODEL,
        "messages": messages,
        "max_tokens": 600,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        ROUTER_URL.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {ROUTER_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=ROUTER_TIMEOUT) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as e:
        log("warn", f"LLM call failed: {e}")
        return None


# ── Action planning ───────────────────────────────────────────────────
ACTION_SCHEMA = """\
Return ONLY a JSON object, no prose. Choose ONE action.

Available actions:
  {"action": "navigate", "url": "https://..."}
  {"action": "click", "selector": "css selector or text"}
  {"action": "click_button", "text": "visible button text"}
  {"action": "click_link", "text": "visible link text"}
  {"action": "type", "selector": "css", "text": "value", "press_enter": false}
  {"action": "fill_first_input", "text": "value"}
  {"action": "screenshot", "label": "describe"}
  {"action": "extract", "what": "title|headings|links|forms|inputs|actions|all"}
  {"action": "wait", "seconds": 2}
  {"action": "finish", "summary": "what you learned"}

Pick "finish" when the goal is achieved or further action won't help.
"""


def plan_rule_based(snap: dict, goal: str, step: int) -> dict:
    """Rule-based fallback: simple keyword matching on goal + vision signals."""
    g = goal.lower()
    title = snap.get("title", "")
    actions = snap.get("actions", [])
    inputs = snap.get("inputs", [])
    links = snap.get("links", [])
    challenge = snap.get("challenge", "none")
    vision = snap.get("vision_verdict", {})  # may be empty

    # If vision says challenge is visible but DOM doesn't, treat as challenge.
    # Vision is supplementary evidence — never override solver authority.
    if challenge == "none" and vision.get("challenge_visible") and vision.get("confidence", 0) >= 0.6:
        log("info", f"plan_rule_based: vision flags hidden challenge "
                    f"hint={vision.get('provider_hint')} conf={vision.get('confidence')}; "
                    f"escalating to wait")
        return {"action": "wait", "seconds": 3,
                "reason": f"vision: {vision.get('provider_hint')} challenge visible"}

    if challenge != "none":
        return {"action": "wait", "seconds": 3}

    if "first heading" in g or "show heading" in g:
        return {"action": "extract", "what": "headings"}
    if "all headings" in g or "list headings" in g:
        return {"action": "extract", "what": "headings"}
    if "title" in g:
        return {"action": "extract", "what": "title"}
    if "all links" in g or "list links" in g:
        return {"action": "extract", "what": "links"}
    if "count link" in g:
        return {"action": "extract", "what": "links"}
    if "forms" in g:
        return {"action": "extract", "what": "forms"}
    if "inputs" in g:
        return {"action": "extract", "what": "inputs"}
    if "screenshot" in g:
        return {"action": "screenshot", "label": f"step{step}"}
    if "first button" in g or "click first button" in g:
        for a in actions:
            if a.get("kind") == "click_button" and a.get("text"):
                return {"action": "click_button", "text": a["text"]}
    if "click" in g:
        # try to extract button/link text from goal
        m = re.search(r'click\s+(?:on\s+)?(?:the\s+)?["\']?([^"\']+)["\']?', g)
        if m:
            text = m.group(1).strip()
            for a in actions:
                if a.get("kind") == "click_button" and a.get("text", "").lower() == text:
                    return {"action": "click_button", "text": a["text"]}
            for l in links:
                if text in l.get("text", "").lower():
                    return {"action": "click_link", "text": l["text"]}
    if "type" in g or "fill" in g:
        m = re.search(r'(?:type|fill)\s+["\']?([^"\']+)["\']?', goal)
        if m and inputs:
            for inp in inputs:
                if inp.get("type", "") in ("text", "search", "email", ""):
                    return {"action": "type", "selector": f"#{inp.get('id') or inp.get('name')}",
                            "text": m.group(1), "press_enter": False}
    if "first link" in g and links:
        return {"action": "click_link", "text": links[0]["text"]}

    # default: extract summary
    return {"action": "extract", "what": "all"}


def plan_llm(snap: dict, goal: str, step: int, history: list[TraceEvent]) -> dict | None:
    """LLM-based action decision. Returns None on failure (caller falls back)."""
    sys_prompt = (
        "You are a careful web agent. You will see a page snapshot and a goal. "
        "Choose ONE next action that best advances the goal. If the goal is "
        "achieved or unachievable, return {\"action\": \"finish\"}. "
        + ACTION_SCHEMA
    )
    # compress snap for prompt
    compact = {
        "url": snap.get("url"),
        "title": snap.get("title"),
        "summary": snap.get("summary", "")[:200],
        "headings": [h.get("text", "")[:60] for h in snap.get("headings", [])[:8]],
        "links_count": len(snap.get("links", [])),
        "links_sample": [l.get("text", "")[:40] for l in snap.get("links", [])[:5]],
        "actions": [{"kind": a.get("kind"), "text": a.get("text", "")[:30]}
                    for a in snap.get("actions", [])[:8]],
        "inputs": [{"name": i.get("name"), "type": i.get("type")}
                   for i in snap.get("inputs", [])[:5]],
        "forms_count": len(snap.get("forms", [])),
        "challenge": snap.get("challenge"),
    }
    # ── vision enrichment ──
    vision = snap.get("vision_verdict", {})
    if vision and vision.get("ran"):
        compact["vision"] = {
            "challenge_visible": vision.get("challenge_visible"),
            "provider_hint": vision.get("provider_hint"),
            "confidence": vision.get("confidence"),
            "sources": vision.get("sources"),
            # truncate OCR text heavily — only a snippet helps the LLM
            "ocr_excerpt": (vision.get("ocr_text", "") or "")[:200],
        }
    user_prompt = f"""\
GOAL: {goal}

STEP: {step}
HISTORY (last 5): {[f"{e.action}({e.target})" for e in history[-5:]]}

SNAPSHOT:
{json.dumps(compact, indent=2)}

Reply with JSON only:
"""
    raw = call_llm(user_prompt, sys_prompt)
    if not raw:
        return None
    # try to extract JSON object
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def plan_hybrid(snap: dict, goal: str, step: int, history: list[TraceEvent],
                use_llm: bool = True) -> dict:
    """Try LLM first, fall back to rule-based."""
    if use_llm:
        llm_action = plan_llm(snap, goal, step, history)
        if llm_action:
            log("info", f"LLM action: {llm_action.get('action')}({llm_action.get('target') or llm_action.get('text') or llm_action.get('what') or llm_action.get('selector') or ''})")
            return llm_action
    rule_action = plan_rule_based(snap, goal, step)
    log("info", f"rule action: {rule_action.get('action')}({rule_action.get('target') or rule_action.get('text') or rule_action.get('what') or rule_action.get('selector') or ''})")
    return rule_action


# ── Execution trace helpers ────────────────────────────────────────────
class TraceWriter:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = RUNS_DIR / run_id
        (self.dir / "screenshots").mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.dir / "trace.jsonl"
        self.fp = open(self.jsonl_path, "a", encoding="utf-8")

    def write(self, event: TraceEvent) -> None:
        self.fp.write(json.dumps(asdict(event)) + "\n")
        self.fp.flush()

    def close(self) -> None:
        self.fp.close()


# ── Safety gate ────────────────────────────────────────────────────────
def check_authorization(url: str, allowed: list[str], mode: str) -> tuple[bool, str]:
    """Verify the URL is on the allow-list and mode is authorized."""
    if mode != "authorized_test":
        return False, f"refused: mode={mode!r} (must be 'authorized_test')"
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not allowed:
        return False, "refused: no domains in allow-list (set allowed_domains)"
    for ok in allowed:
        if host == ok or host.endswith("." + ok):
            return True, f"ok: {host} matches allow-list"
    return False, f"refused: {host} not in allow-list {allowed}"


# ── Main agent class ───────────────────────────────────────────────────
class AIWebAgent:
    """Web agent that uses cf_selenium to act on pages per a goal.

    Loop:
      1. snapshot()
      2. plan next action
      3. before-screenshot
      4. execute action
      5. after-screenshot
      6. snapshot() again, detect change
      7. handle challenge if detected
      8. write trace event
      9. repeat until finish or max_steps
    """

    def __init__(self, profile: str = "agent",
                 max_steps: int = 10,
                 allowed_domains: list[str] | None = None,
                 authorized_test_mode: bool = True,
                 use_llm: bool = True,
                 use_vision: bool | None = None,
                 headless: bool = True,
                 wait_per_step: float = 0.5):
        self.profile = profile
        self.max_steps = max_steps
        self.allowed_domains = allowed_domains if allowed_domains is not None else list(ALLOWED_DOMAINS_DEFAULT)
        self.authorized_test_mode = authorized_test_mode
        self.use_llm = use_llm and bool(ROUTER_KEY)
        # Vision: env-var override; default OFF (consistent with CF_BYPASS_ENABLED).
        if use_vision is None:
            use_vision = os.environ.get("CF_VISION_ENABLED", "").lower() in ("1", "true", "yes", "on")
        self.use_vision = bool(use_vision)
        self.headless = headless
        self.wait_per_step = wait_per_step
        self.run_id = f"run_{int(time.time())}_{os.urandom(2).hex()}"
        self.tracer = TraceWriter(self.run_id)
        self.history: list[TraceEvent] = []
        self.tried: set[tuple[str, str]] = set()
        self.last_sig = ""
        self.last_snap: dict = {}
        self.b: Browser | None = None
        self.human_required_error: Exception | None = None
        self.current_provider: str = ""
        self.current_detection: dict = {}
        # vision results: keyed by step index
        self.vision_log: list[dict] = []

    # ── screenshot helpers ────────────────────────────────
    def _shot(self, label: str) -> str | None:
        if not self.b:
            return None
        try:
            path = self.b.screenshot(
                str(self.tracer.dir / "screenshots" / f"{label}.png")
            )
            return str(path)
        except Exception as e:
            log("warn", f"screenshot {label} failed: {e}")
            return None

    # ── vision consult (decision layer only — NEVER solves) ─
    def vision_consult(self, step_idx: int, snap: dict, png_path: str | None) -> dict:
        """Call vision providers to classify the current page state.

        Vision is decision-only:
          - confirms/denies the DOM's challenge classification
          - extracts OCR text the planner can use as a richer signal
          - NEVER auto-solves, never clicks, never types — only observes.

        Returns a verdict dict:
          {
            "ran": bool,
            "challenge_visible": bool,        # vision says a challenge is showing
            "provider_hint": str,             # "cloudflare" | "recaptcha" | "none" | "unknown"
            "confidence": float,             # 0.0–1.0
            "ocr_text": str,                  # Tesseract extract, first ~500 chars
            "sources": list[str],             # which providers ran successfully
            "latency_ms": int,
            "error": str | None,
          }
        """
        empty = {
            "ran": False, "challenge_visible": False, "provider_hint": "none",
            "confidence": 0.0, "ocr_text": "", "sources": [],
            "latency_ms": 0, "error": None,
        }
        if not self.use_vision:
            return {**empty, "error": "vision disabled (CF_VISION_ENABLED=OFF)"}
        if png_path is None or not Path(png_path).exists():
            return {**empty, "error": f"png not found: {png_path}"}
        try:
            from specter.vision import analyze_page
        except Exception as e:
            return {**empty, "error": f"import vision failed: {e}"}
        t0 = time.time()
        host = ""
        try:
            from urllib.parse import urlparse as _urlparse
            host = _urlparse(snap.get("url", "")).hostname or ""
        except Exception:
            pass
        try:
            # enrich snap with HTML for DOM provider (cf_selenium.snapshot() doesn't include it)
            if "html" not in snap and self.b is not None:
                try:
                    snap = dict(snap)  # shallow copy
                    snap["html"] = self.b.html or ""  # property, not method
                except Exception as e:
                    log("warn", f"could not fetch HTML for vision: {e}")
            res = analyze_page(
                snapshot=snap, png_path=png_path, host=host,
                use_tesseract=True, use_claude=False, use_dom=True,
            )
        except Exception as e:
            return {**empty, "error": f"analyze_page failed: {e}"}
        latency = int((time.time() - t0) * 1000)
        # res is dict[str, VisionResult]
        dom_r = res.get("dom")
        tess_r = res.get("tesseract")
        claude_r = res.get("claude")
        sources = []
        # DOM verdict (most reliable for structural challenge detection)
        dom_label = ""
        dom_conf = 0.0
        if dom_r and not getattr(dom_r, "error", None):
            dom_label = dom_r.label or "none"
            dom_conf = dom_r.confidence
            sources.append("dom")
        # Tesseract verdict (text signal)
        tess_text = ""
        if tess_r and not getattr(tess_r, "error", None):
            tess_text = tess_r.text or ""
            sources.append("tesseract")
        if claude_r and not getattr(claude_r, "error", None):
            sources.append("claude")
        # Decide "challenge_visible": DOM says a challenge label with conf >= 0.6
        challenge_visible = False
        provider_hint = "none"
        confidence = 0.0
        if dom_label and dom_label not in ("none", "normal_page", "unknown", ""):
            if dom_conf >= 0.6:
                challenge_visible = True
                provider_hint = dom_label
                confidence = dom_conf
        # Tesseract can also surface challenge keywords (weak signal)
        if not challenge_visible and tess_text:
            tl = tess_text.lower()
            for kw, prov in [("cloudflare", "cloudflare"), ("cf-challenge", "cloudflare"),
                              ("recaptcha", "recaptcha"), ("hcaptcha", "hcaptcha"),
                              ("h-captcha", "hcaptcha"), ("arkose", "arkose"),
                              ("aws-waf", "aws_waf"), ("awswaf", "aws_waf"),
                              ("just a moment", "cloudflare"), ("checking your browser", "cloudflare")]:
                if kw in tl:
                    challenge_visible = True
                    provider_hint = prov
                    confidence = max(confidence, 0.5)  # text-only is weaker
                    break
        verdict = {
            "ran": True,
            "challenge_visible": challenge_visible,
            "provider_hint": provider_hint,
            "confidence": round(confidence, 3),
            "ocr_text": tess_text[:500] if tess_text else "",
            "sources": sources,
            "latency_ms": latency,
            "error": None,
        }
        verdict["step"] = step_idx
        verdict["host"] = host
        self.vision_log.append(verdict)
        log("info", f"vision step={step_idx} challenge_visible={challenge_visible} "
                    f"hint={provider_hint} conf={confidence:.2f} sources={sources} "
                    f"latency={latency}ms")
        return verdict

    # ── per-step execution ─────────────────────────────────
    def _execute(self, plan: dict) -> tuple[str, str, str]:
        """Run one plan. Returns (action_name, target_desc, result_text)."""
        action = plan.get("action", "wait")
        target_desc = ""

        if action == "navigate":
            url = plan.get("url", "")
            target_desc = url
            self.b.get(url, wait_for="body")
            return action, url, f"navigated to {url}"

        if action == "click":
            sel = plan.get("selector", "")
            target_desc = sel
            el = self.b.find_element(sel)
            self.b._run(el.click())  # sync wrapper for coroutine
            return action, sel, f"clicked {sel}"

        if action == "click_button":
            text = plan.get("text", "")
            target_desc = f"button:{text}"
            # xkiro.com and many modern sites use <button> with text content
            # but no aria-label. Try role-based first, fall back to text search.
            try:
                el = self.b.find_by_role("button", text)
                self.b._run(el.click())  # sync wrapper for coroutine
            except RuntimeError:
                # fall back to find_by_text (selects first element containing text)
                try:
                    el = self.b.find_by_text(text, tag="button")
                    self.b._run(el.click())  # sync wrapper for coroutine
                except RuntimeError:
                    # try any element (input[type=submit], div, span, etc.)
                    el = self.b.find_by_text(text)
                    self.b._run(el.click())  # sync wrapper for coroutine
            return action, text, f"clicked button {text!r}"

        if action == "click_link":
            text = plan.get("text", "")
            target_desc = f"link:{text}"
            el = self.b.find_by_text(text)
            self.b._run(el.click())  # sync wrapper for coroutine
            return action, text, f"clicked link {text!r}"

        if action == "type":
            sel = plan.get("selector", "")
            text = plan.get("text", "")
            enter = plan.get("press_enter", False)
            target_desc = f"{sel}={text!r}"
            el = self.b.find_element(sel)
            self.b._run(el.type(text))  # sync wrapper for coroutine
            if enter:
                el.key("Enter")
            return action, target_desc, f"typed {text!r} into {sel}"

        if action == "fill_first_input":
            text = plan.get("text", "")
            target_desc = f"first-input={text!r}"
            el = self.b.find_element("input[type='text'],input:not([type]),textarea")
            self.b._run(el.type(text))  # sync wrapper for coroutine
            return action, target_desc, f"filled first input with {text!r}"

        if action == "screenshot":
            label = plan.get("label", "manual")
            target_desc = f"screenshot:{label}"
            path = self._shot(label)
            return action, target_desc, f"screenshot saved: {path}"

        if action == "extract":
            what = plan.get("what", "all")
            target_desc = f"extract:{what}"
            data = self._extract(what)
            return action, target_desc, json.dumps(data, ensure_ascii=False)[:500]

        if action == "wait":
            s = float(plan.get("seconds", 1))
            target_desc = f"wait:{s}s"
            time.sleep(s)
            return action, target_desc, f"waited {s}s"

        if action == "finish":
            target_desc = "finish"
            return action, target_desc, plan.get("summary", "done")

        return action, target_desc, f"unknown action: {action}"

    def _extract(self, what: str) -> Any:
        snap = self.last_snap
        if what == "all":
            return {k: snap.get(k) for k in
                    ("title", "url", "headings", "links", "actions",
                     "forms", "inputs", "challenge", "cookies_count")}
        if what in snap:
            return snap[what]
        return snap

    # ── challenge handling ─────────────────────────────────
    def _handle_challenge(self) -> dict:
        """Run challenge solver if detected, using multi-provider registry.

        Flow:
          1. Run ProviderDetector on current snapshot (browser-side).
          2. If a provider is identified with confidence > threshold:
             - If auto_solvable: call adapter.solve(b, url)
             - If human_required: raise HumanRequiredError (caught by run loop)
             - If ProviderNotSolvableError: log and continue (b may auto-solve)
          3. Re-navigate; cf_selenium auto-solves in get() as fallback.
        """
        snap = self.last_snap
        if snap.get("challenge") == "none":
            return snap
        challenge = snap.get("challenge", "none")
        log("info", f"challenge detected: {challenge}; running solver flow")

        # ── vision consult (decision only) ──────────────────
        # If vision is enabled, take a screenshot and ask the vision providers
        # whether a challenge is actually still visible. Vision NEVER solves —
        # it only advises. We use it to confirm the DOM signal and to catch
        # cases where the DOM says "none" but the page is still showing a spinner.
        vision_verdict: dict = {}
        if self.use_vision:
            try:
                shot = self._shot(f"vision_step_before_solve")
                verdict = self.vision_consult(step_idx=-1, snap=snap, png_path=shot)
                vision_verdict = verdict
                # If DOM says challenge but vision disagrees, log but still attempt solver.
                if verdict.get("ran") and not verdict.get("challenge_visible"):
                    log("warn", f"vision: DOM says challenge={challenge} but vision says not visible "
                                f"(hint={verdict.get('provider_hint')} conf={verdict.get('confidence')})")
                elif verdict.get("ran") and verdict.get("challenge_visible"):
                    log("info", f"vision: confirms challenge visible (hint={verdict.get('provider_hint')} "
                                f"conf={verdict.get('confidence')})")
            except Exception as e:
                log("warn", f"vision consult failed: {e}")
                vision_verdict = {"error": str(e)}

        # multi-provider detection
        try:
            from specter.providers import (
                get_detector, get_registry, ProviderId, ChallengeState,
                HumanRequiredError, ProviderNotSolvableError,
            )
            detector = get_detector()
            registry = get_registry()
            host = snap.get("url", "").split("/")[2] if "://" in snap.get("url", "") else ""
            det = detector.detect_browser(snap, host=host)
            if det.provider != ProviderId.UNKNOWN and det.confidence >= 0.6:
                log("info", f"detected provider={det.provider} conf={det.confidence:.2f}")
                adapter = registry.get(det.provider)
                if adapter is not None:
                    if not adapter.can_handle(det.challenge_state):
                        log("warn", f"adapter {det.provider} cannot handle state={det.challenge_state}")
                    elif adapter.auto_solvable and self.b is not None:
                        try:
                            adapter.solve(self.b, self.b.url)
                        except (ProviderNotSolvableError, NotImplementedError) as e:
                            log("warn", f"adapter.solve not implemented: {e}")
                        except HumanRequiredError as e:
                            log("error", f"HUMAN REQUIRED: {e}; stop agent")
                            self.human_required_error = e
                    elif det.challenge_state == ChallengeState.HUMAN_REQUIRED:
                        # cannot auto-solve; flag for stop
                        e = HumanRequiredError(
                            f"provider={det.provider} requires human intervention",
                            provider=det.provider,
                            hint=f"open {self.b.url if self.b else 'URL'} in real browser",
                        )
                        log("error", f"{e}")
                        self.human_required_error = e
        except Exception as e:
            log("warn", f"provider detection error: {e}")

        # re-navigate as fallback (cf_selenium may auto-solve in get())
        if self.b:
            try:
                self.b.get(self.b.url, wait_for="body")
            except Exception as e:
                log("warn", f"re-navigate failed: {e}")
            time.sleep(2)
            new_snap = self.b.snapshot(include_html=True)
            if new_snap.get("challenge") == "none":
                log("info", f"challenge resolved; cookies={new_snap.get('cookies_count')}")
            else:
                log("warn", f"challenge still present: {new_snap.get('challenge')}")
            # attach vision verdict so the planner can use it next step
            if vision_verdict:
                new_snap["vision_verdict"] = vision_verdict
            return new_snap
        if vision_verdict:
            snap["vision_verdict"] = vision_verdict
        return snap

    # ── main run loop ─────────────────────────────────────
    def run(self, url: str, goal: str) -> AgentResult:
        log("info", f"=== run start: run_id={self.run_id} ===")
        log("info", f"url={url} goal={goal!r} max_steps={self.max_steps}")
        log("info", f"authorized_test_mode={self.authorized_test_mode} "
                    f"allowed_domains={self.allowed_domains} use_llm={self.use_llm}")

        # auth check
        ok, reason = check_authorization(url, self.allowed_domains,
                                         "authorized_test" if self.authorized_test_mode else "production")
        if not ok:
            log("error", reason)
            self.tracer.close()
            return AgentResult(
                success=False, run_id=self.run_id, start_url=url,
                final_url="", final_title="", goal=goal, steps=[],
                summary=reason, error=reason,
                artifacts_dir=str(self.tracer.dir),
            )
        log("info", reason)

        # open browser
        try:
            self.b = Browser(profile=self.profile, headless=self.headless, auto_solve=True)
        except Exception as e:
            log("error", f"browser launch failed: {e}")
            self.tracer.close()
            return AgentResult(
                success=False, run_id=self.run_id, start_url=url,
                final_url="", final_title="", goal=goal, steps=[],
                summary=f"browser launch failed: {e}", error=str(e),
                artifacts_dir=str(self.tracer.dir),
            )

        summary = ""
        error: str | None = None
        success = False
        try:
            # step 0: navigate to start URL
            log("info", f"step 0: navigate {url}")
            self.b.get(url, wait_for="body")
            self._shot("step0_after_navigate")
            snap = self.b.snapshot(include_html=True)
            self.last_snap = snap
            self.last_sig = page_signature(snap)

            # multi-provider detection on initial snapshot
            try:
                from specter.providers import get_detector, ProviderId
                from urllib.parse import urlparse as _urlparse
                det = get_detector()
                host = _urlparse(snap.get("url", url)).hostname or ""
                det_result = det.detect_browser(snap, host=host)
                self.current_provider = det_result.provider
                self.current_detection = det_result.to_dict()
                if det_result.provider != ProviderId.UNKNOWN:
                    log("info", f"detected provider={det_result.provider} conf={det_result.confidence:.2f} state={det_result.challenge_state}")
            except Exception as e:
                log("warn", f"initial provider detection failed: {e}")
                self.current_provider = ""
                self.current_detection = {}

            # handle initial challenge if any
            if snap.get("challenge") != "none":
                snap = self._handle_challenge()
                self.last_snap = snap
                self.last_sig = page_signature(snap)
                # check if human required
                if self.human_required_error is not None:
                    log("error", f"aborting: {self.human_required_error}")
                    self.tracer.close()
                    return AgentResult(
                        success=False, run_id=self.run_id, start_url=url,
                        final_url=snap.get("url", ""), final_title=snap.get("title", ""),
                        goal=goal, steps=self.history,
                        summary=f"human intervention required for {self.current_provider}",
                        error=str(self.human_required_error),
                        artifacts_dir=str(self.tracer.dir),
                    )

            # log step 0 event
            ev = TraceEvent(
                step=0, action="navigate", target=url, url=snap.get("url", ""),
                timestamp=_ts(), result=f"title={snap.get('title','')!r}",
                extra={
                    "challenge": snap.get("challenge"),
                    "cookies": snap.get("cookies_count"),
                    "provider": self.current_provider,
                    "detection": self.current_detection,
                },
            )
            self.tracer.write(ev)
            self.history.append(ev)

            # ── initial vision consult (step 0) ──
            if self.use_vision and self.b is not None:
                try:
                    shot0 = self.tracer.dir / "screenshots" / "step0_after_navigate.png"
                    v0 = self.vision_consult(step_idx=0, snap=snap, png_path=str(shot0))
                    if v0.get("ran"):
                        snap["vision_verdict"] = v0
                        self.last_snap = snap
                        # log step 0 vision as a separate event
                        ev_v = TraceEvent(
                            step=0, action="vision_consult", target=str(shot0),
                            url=snap.get("url", ""), timestamp=_ts(),
                            result=f"hint={v0.get('provider_hint')} conf={v0.get('confidence')}",
                            extra={"vision": {
                                "ran": True,
                                "challenge_visible": v0.get("challenge_visible"),
                                "provider_hint": v0.get("provider_hint"),
                                "confidence": v0.get("confidence"),
                                "sources": v0.get("sources", []),
                                "latency_ms": v0.get("latency_ms", 0),
                            }},
                        )
                        self.tracer.write(ev_v)
                        self.history.append(ev_v)
                except Exception as e:
                    log("warn", f"step-0 vision consult failed: {e}")

            # ── main loop ───────────────────────────────────
            for step in range(1, self.max_steps + 1):
                log("info", f"--- step {step} ---")

                # plan (skip same action+target already tried if page unchanged)
                plan = plan_hybrid(self.last_snap, goal, step, self.history, self.use_llm)
                plan_action = plan.get("action", "wait")
                plan_target = (plan.get("text") or plan.get("selector") or
                               plan.get("url") or plan.get("what") or plan.get("label") or "")
                plan_key = (plan_action, str(plan_target))
                # dedup vs previous step's (not all history) — only if page didn't change
                if (self.history and self.history[-1].extra.get("page_changed") is False
                        and (self.history[-1].action, self.history[-1].target) == plan_key):
                    log("info", f"already tried {plan_key} and page unchanged, escalating to extract")
                    plan = {"action": "extract", "what": "all"}
                    plan_action, plan_target = "extract", "all"
                self.tried.add(plan_key)

                # before-shot
                self._shot(f"step{step}_before")

                # ── vision consult (decision layer, per-step) ──
                # Run after the screenshot so vision gets a fresh page state.
                # Attaches verdict to last_snap so the planner uses it next iteration.
                # The verdict for THIS step also gets written to the trace event below.
                step_vision_verdict: dict = {}
                if self.use_vision and self.b is not None:
                    try:
                        shot_path = self.tracer.dir / "screenshots" / f"step{step}_before.png"
                        step_vision_verdict = self.vision_consult(
                            step_idx=step, snap=self.last_snap, png_path=str(shot_path)
                        )
                        if step_vision_verdict.get("ran"):
                            self.last_snap["vision_verdict"] = step_vision_verdict
                    except Exception as e:
                        log("warn", f"per-step vision consult failed: {e}")
                        step_vision_verdict = {"error": str(e)}

                # execute
                t0 = time.time()
                try:
                    act, target, result = self._execute(plan)
                except Exception as e:
                    log("error", f"action failed: {e}")
                    act, target, result = plan_action, str(plan), f"ERROR: {e}"
                    error = str(e)
                dur = int((time.time() - t0) * 1000)

                # small settle
                time.sleep(self.wait_per_step)

                # snapshot again
                new_snap = self.b.snapshot(include_html=True)
                new_sig = page_signature(new_snap)
                changed = new_sig != self.last_sig

                # handle challenge if appeared
                if new_snap.get("challenge") != "none":
                    new_snap = self._handle_challenge()
                    new_sig = page_signature(new_snap)

                # after-shot
                self._shot(f"step{step}_after")

                # check if human intervention was triggered
                if self.human_required_error is not None:
                    log("error", f"aborting step {step}: {self.human_required_error}")
                    ev = TraceEvent(
                        step=step, action=act, target=target, url=new_snap.get("url", ""),
                        timestamp=_ts(), result=f"ABORTED: {self.human_required_error}",
                        duration_ms=dur,
                        extra={
                            "page_changed": changed,
                            "old_sig": self.last_sig, "new_sig": new_sig,
                            "challenge": new_snap.get("challenge"),
                            "cookies": new_snap.get("cookies_count"),
                            "provider": self.current_provider,
                            "aborted_reason": "human_required",
                        },
                    )
                    self.tracer.write(ev)
                    self.history.append(ev)
                    summary = f"human intervention required for {self.current_provider}"
                    error = str(self.human_required_error)
                    success = False
                    break

                # trace event
                ev = TraceEvent(
                    step=step, action=act, target=target, url=new_snap.get("url", ""),
                    timestamp=_ts(), result=result[:300], duration_ms=dur,
                    extra={
                        "page_changed": changed,
                        "old_sig": self.last_sig, "new_sig": new_sig,
                        "challenge": new_snap.get("challenge"),
                        "cookies": new_snap.get("cookies_count"),
                        "provider": self.current_provider,
                        # vision verdict: short summary (avoid bloating trace JSON)
                        "vision": {
                            "ran": step_vision_verdict.get("ran", False),
                            "challenge_visible": step_vision_verdict.get("challenge_visible"),
                            "provider_hint": step_vision_verdict.get("provider_hint"),
                            "confidence": step_vision_verdict.get("confidence"),
                            "sources": step_vision_verdict.get("sources", []),
                            "latency_ms": step_vision_verdict.get("latency_ms", 0),
                        } if step_vision_verdict else None,
                    },
                )
                self.tracer.write(ev)
                self.history.append(ev)

                self.last_snap = new_snap
                self.last_sig = new_sig

                # finish?
                if plan_action == "finish" or plan_action == "extract":
                    # if extract: result is the data, decide if done
                    if plan_action == "finish":
                        summary = plan.get("summary", "task done")
                        success = True
                        log("info", f"agent finished: {summary}")
                        break
                    # for extract, just continue unless goal explicitly done
                    if step >= self.max_steps:
                        summary = f"max_steps reached, last extract: {result[:200]}"
                        success = True
                        break
        finally:
            try:
                if self.b:
                    self.b.quit()
            except Exception:
                pass
            self.tracer.close()

        # write summary file
        result = AgentResult(
            success=success, run_id=self.run_id, start_url=url,
            final_url=self.last_snap.get("url", ""),
            final_title=self.last_snap.get("title", ""),
            goal=goal, steps=self.history, summary=summary,
            error=error, artifacts_dir=str(self.tracer.dir),
        )
        result_dict = asdict(result)
        # attach vision_log (not part of AgentResult dataclass)
        result_dict["vision_log"] = self.vision_log
        (RUNS_DIR / self.run_id / "result.json").write_text(
            json.dumps(result_dict, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log("info", f"=== run end: {self.run_id} success={success} steps={len(self.history)} ===")
        return result

    # context manager sugar
    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            if self.b:
                self.b.quit()
        except Exception:
            pass
        self.tracer.close()


# ── CLI ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="AI web-agent with CF bypass (authorized_test_mode).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("url", nargs="?", help="Start URL")
    ap.add_argument("goal", nargs="?", help="User goal (string)")
    ap.add_argument("--task", help="Path to task JSON file")
    ap.add_argument("--max-steps", type=int, default=10)
    ap.add_argument("--profile", default="agent")
    ap.add_argument("--allow", action="append", default=[],
                    help="Allowed domain (can repeat). Required.")
    ap.add_argument("--no-llm", action="store_true", help="Force rule-based only")
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    # input
    if args.task:
        task = json.loads(Path(args.task).read_text())
        url = task["url"]
        goal = task.get("goal", "")
        max_steps = task.get("max_steps", args.max_steps)
        profile = task.get("profile", args.profile)
        allowed = task.get("allowed_domains", args.allow)
        no_llm = task.get("no_llm", args.no_llm)
    else:
        if not args.url or not args.goal:
            ap.error("provide url+goal, or --task task.json")
        url = args.url
        goal = args.goal
        max_steps = args.max_steps
        profile = args.profile
        allowed = args.allow
        no_llm = args.no_llm

    if not allowed:
        print("ERROR: --allow is required (authorized_test_mode).", file=sys.stderr)
        print("Example: --allow staging.example.com --allow localhost", file=sys.stderr)
        sys.exit(2)

    agent = AIWebAgent(
        profile=profile,
        max_steps=max_steps,
        allowed_domains=allowed,
        authorized_test_mode=True,
        use_llm=not no_llm,
        headless=args.headless,
    )
    result = agent.run(url, goal)

    # print to stdout
    print(json.dumps({
        "success": result.success,
        "run_id": result.run_id,
        "artifacts_dir": result.artifacts_dir,
        "final_url": result.final_url,
        "final_title": result.final_title,
        "summary": result.summary,
        "error": result.error,
        "steps": [asdict(s) for s in result.steps],
    }, indent=2, ensure_ascii=False))

    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
