"""hcaptcha_solver_adapter.py — hCaptcha adapter WITH auto-solve via browser automation.

This is an UPGRADED version of hcaptcha_adapter.py that:
  1. Detects hCaptcha (same as original)
  2. Auto-solves via cf_selenium Browser:
     - Navigate to the page
     - Wait for hCaptcha iframe
     - Click the checkbox (or trigger invisible flow)
     - Wait for the h-captcha-response token to be generated
     - Extract the token
  3. Falls back to HumanRequiredError if auto-solve fails

This stays consistent with SPECTER's frozen modules: cf_selenium is the browser,
hcaptcha is just an adapter that uses it. We do NOT touch cf_selenium.py.

Configuration:
  SOLVER_MODE = "auto"        # "auto" | "manual" | "fallback_only"
  WAIT_TIMEOUT = 90            # seconds to wait for challenge solve
  USE_TESSERACT_OCR = False    # disabled by default (hCaptcha image challenges
                                # need real human vision; Tesseract won't help
                                # with "click all images with X" challenges)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .base import (
    ChallengeState,
    DetectionResult,
    HumanRequiredError,
    ProviderId,
    ProtectionProvider,
)
from .hcaptcha_adapter import HCaptchaAdapter  # inherit detection


class HCaptchaSolverAdapter(HCaptchaAdapter):
    """hCaptcha adapter with auto-solve capability via cf_selenium.

    Strategy:
      1. Detect hCaptcha presence (inherited from HCaptchaAdapter).
      2. On solve(): wait for h-captcha-response textarea to be populated.
         This works for two common patterns:
         a) hCaptcha invisible: the token is set automatically after JS
            challenges (e.g. proof-of-work, simple click).
         b) hCaptcha visible with simple click: clicking the checkbox
            triggers a JS challenge that the cf_selenium stealth layer
            (human_mouse_move + delay) helps pass.
         For complex image challenges ("click all images with X"),
         this adapter raises HumanRequiredError after WAIT_TIMEOUT.
    """

    provider_id = ProviderId.HCAPTCHA
    display_name = "hCaptcha (auto-solver)"
    auto_solvable = True  # ← changed from False; we attempt solve

    # Tunables
    DEFAULT_TIMEOUT = 90  # seconds
    CHECKBOX_IFRAME_SELECTOR = "iframe[src*='hcaptcha.com/captcha/v1']"
    RESPONSE_FIELD_JS = """
    (() => {
      const ta = document.querySelector('textarea[name="h-captcha-response"]');
      return ta ? ta.value : '';
    })()
    """

    def can_handle(self, state: str) -> bool:
        return state in (ChallengeState.NONE, ChallengeState.CAPTCHA, ChallengeState.HUMAN_REQUIRED)

    def solve(self, browser, url: str) -> dict:
        """Attempt to auto-solve hCaptcha and return the h-captcha-response token.

        Returns dict with:
          - token: the h-captcha-response value (or "" if not obtained)
          - cookies: list of cookies set during solve
          - user_agent: from the browser
          - extra: {sitekey, response_field_selector, wait_time, attempts}

        Raises:
          HumanRequiredError: if the challenge is too complex (image grid)
                              or timeout exceeded
        """
        if browser is None:
            raise HumanRequiredError(
                "hCaptcha solver requires a browser instance",
                provider=self.provider_id,
                hint="Pass a cf_selenium.Browser with auto_solve=True"
            )

        # 1. Make sure we're on the page
        try:
            current_url = browser.url if hasattr(browser, "url") else url
        except Exception:
            current_url = url

        # 2. Try to extract the sitekey from the page
        sitekey = ""
        try:
            js_sitekey = """
            (() => {
              const el = document.querySelector('[data-hcaptcha-sitekey]') ||
                         document.querySelector('.h-captcha[data-sitekey]');
              return el ? el.getAttribute('data-hcaptcha-sitekey') : '';
            })()
            """
            if hasattr(browser, "execute_script"):
                sitekey = browser.execute_script(js_sitekey) or ""
            elif hasattr(browser, "evaluate"):
                sitekey = browser._run(browser._cdp.js(js_sitekey, timeout=5)) or ""
        except Exception:
            pass

        # 3. Wait for the h-captcha-response to be populated
        timeout = self.DEFAULT_TIMEOUT
        t0 = time.time()
        token = ""
        attempts = 0
        last_token_len = 0

        while time.time() - t0 < timeout:
            attempts += 1
            try:
                if hasattr(browser, "execute_script"):
                    token = browser.execute_script(self.RESPONSE_FIELD_JS) or ""
                else:
                    token = browser._run(browser._cdp.js(self.RESPONSE_FIELD_JS, timeout=5)) or ""
            except Exception:
                token = ""

            if token and len(token) > 20:  # valid h-captcha-response is ~600+ chars
                # Got a real token
                break

            # If token is empty, try clicking the hCaptcha checkbox iframe
            if attempts == 2 or (attempts % 5 == 0 and time.time() - t0 > 5):
                try:
                    self._click_hcaptcha_checkbox(browser)
                except Exception:
                    pass  # checkbox may not be visible yet

            # Adaptive sleep — slow down if nothing's changing
            elapsed = time.time() - t0
            if attempts < 5:
                sleep_s = 1.0
            elif attempts < 15:
                sleep_s = 2.0
            else:
                sleep_s = 3.0
            time.sleep(sleep_s)

        # 4. Build result
        cookies = []
        try:
            if hasattr(browser, "cookies"):
                ck = browser.cookies
                if isinstance(ck, dict):
                    cookies = [{"name": k, "value": v} for k, v in ck.items()]
        except Exception:
            pass

        ua = ""
        try:
            if hasattr(browser, "execute_script"):
                ua = browser.execute_script("return navigator.userAgent") or ""
        except Exception:
            pass

        if not token or len(token) < 20:
            raise HumanRequiredError(
                f"hCaptcha auto-solve timed out after {int(time.time() - t0)}s "
                f"(attempts={attempts}, sitekey={sitekey[:16]}...). "
                f"This usually means a complex image challenge that needs human vision.",
                provider=self.provider_id,
                hint="Open the page in a real browser and solve the image grid, "
                     "or use a solver API (2Captcha, Anti-Captcha)."
            )

        return {
            "token": token,
            "cookies": cookies,
            "user_agent": ua,
            "fingerprint": {},
            "extra": {
                "sitekey": sitekey,
                "wait_time": int(time.time() - t0),
                "attempts": attempts,
                "token_length": len(token),
            },
        }

    def _click_hcaptcha_checkbox(self, browser) -> None:
        """Try to click the hCaptcha checkbox inside its iframe.

        hCaptcha renders inside a cross-origin iframe, so we need to:
          1. Find the iframe via DOM
          2. Switch to it via CDP Target
          3. Click the checkbox
        """
        # First check if the iframe is even present
        js = """
        (() => {
          const ifr = document.querySelector("iframe[src*='hcaptcha.com']");
          if (!ifr) return null;
          const r = ifr.getBoundingClientRect();
          return JSON.stringify({
            x: r.x, y: r.y, w: r.width, h: r.height,
            src: ifr.src,
            visible: r.width > 0 && r.height > 0
          });
        })()
        """
        try:
            if hasattr(browser, "execute_script"):
                raw = browser.execute_script(js)
            else:
                raw = browser._run(browser._cdp.js(js, timeout=5))
        except Exception:
            return

        if not raw or raw == "null" or raw == "None":
            return

        import json as _json
        try:
            info = _json.loads(raw)
        except Exception:
            return

        if not info.get("visible"):
            return

        # hCaptcha checkbox is usually in the center of the iframe
        cx = info["x"] + info["w"] / 2
        cy = info["y"] + info["h"] / 2

        # Use cf_selenium's human_click for natural mouse movement
        try:
            if hasattr(browser, "_cdp"):
                # Direct CDP click
                from cf_selenium import human_click  # local import
                # human_click is async; need to run it
                browser._run(human_click(browser._cdp, cx, cy))
        except Exception:
            # Fallback: direct mouse event
            try:
                browser._run(browser._cdp.cmd("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": cx, "y": cy,
                    "button": "left", "buttons": 1, "clickCount": 1,
                }, timeout=3))
                browser._run(browser._cdp.cmd("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": cx, "y": cy,
                    "button": "left", "buttons": 0, "clickCount": 1,
                }, timeout=3))
            except Exception:
                pass
