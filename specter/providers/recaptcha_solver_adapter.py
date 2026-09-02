"""recaptcha_solver_adapter.py — Google reCAPTCHA adapter WITH auto-solve.

Same pattern as hcaptcha_solver_adapter but for reCAPTCHA v2/v3.
"""
from __future__ import annotations

import time
from typing import Any

from .base import (
    ChallengeState,
    HumanRequiredError,
    ProviderId,
)
from .recaptcha_adapter import ReCaptchaAdapter  # inherit detection


class ReCaptchaSolverAdapter(ReCaptchaAdapter):
    """reCAPTCHA adapter with auto-solve via browser.

    Strategy:
      1. Detect reCAPTCHA (inherited from ReCaptchaAdapter).
      2. On solve():
         a) reCAPTCHA v3: token is set automatically after page load
            (no user interaction needed if score is good).
         b) reCAPTCHA v2 invisible: similar, token auto-set.
         c) reCAPTCHA v2 visible ("I'm not a robot"): click the checkbox,
            then wait for the g-recaptcha-response token.
         For image challenges (v2 "select all images with X"), fall back
         to HumanRequiredError after timeout.
    """

    provider_id = ProviderId.RECAPTCHA
    display_name = "Google reCAPTCHA (auto-solver)"
    auto_solvable = True

    DEFAULT_TIMEOUT = 60  # reCAPTCHA usually faster than hCaptcha

    RESPONSE_FIELD_JS = """
    (() => {
      const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
      return ta ? ta.value : '';
    })()
    """

    def can_handle(self, state: str) -> bool:
        return state in (ChallengeState.NONE, ChallengeState.CAPTCHA, ChallengeState.HUMAN_REQUIRED)

    def solve(self, browser, url: str) -> dict:
        if browser is None:
            raise HumanRequiredError(
                "reCAPTCHA solver requires a browser instance",
                provider=self.provider_id,
                hint="Pass a cf_selenium.Browser with auto_solve=True"
            )

        # Extract sitekey
        sitekey = ""
        try:
            js_sitekey = """
            (() => {
              const el = document.querySelector('.g-recaptcha[data-sitekey]') ||
                         document.querySelector('[data-sitekey]');
              return el ? el.getAttribute('data-sitekey') : '';
            })()
            """
            if hasattr(browser, "execute_script"):
                sitekey = browser.execute_script(js_sitekey) or ""
        except Exception:
            pass

        # Wait for g-recaptcha-response
        timeout = self.DEFAULT_TIMEOUT
        t0 = time.time()
        token = ""
        attempts = 0

        while time.time() - t0 < timeout:
            attempts += 1
            try:
                if hasattr(browser, "execute_script"):
                    token = browser.execute_script(self.RESPONSE_FIELD_JS) or ""
            except Exception:
                token = ""

            if token and len(token) > 20:
                break

            # Try clicking the reCAPTCHA checkbox
            if attempts == 2 or (attempts % 5 == 0 and time.time() - t0 > 5):
                try:
                    self._click_recaptcha_checkbox(browser)
                except Exception:
                    pass

            elapsed = time.time() - t0
            if attempts < 5:
                sleep_s = 1.0
            elif attempts < 15:
                sleep_s = 2.0
            else:
                sleep_s = 3.0
            time.sleep(sleep_s)

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
                f"reCAPTCHA auto-solve timed out after {int(time.time() - t0)}s. "
                f"Image grid challenges need human vision.",
                provider=self.provider_id,
                hint="Open in real browser, click 'I'm not a robot', solve images, "
                     "or use 2Captcha/Anti-Captcha API."
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

    def _click_recaptcha_checkbox(self, browser) -> None:
        """Click the reCAPTCHA checkbox inside its iframe."""
        js = """
        (() => {
          const ifr = document.querySelector("iframe[src*='recaptcha']");
          if (!ifr) return null;
          const r = ifr.getBoundingClientRect();
          return JSON.stringify({
            x: r.x, y: r.y, w: r.width, h: r.height,
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

        if not raw or raw == "null":
            return

        import json as _json
        try:
            info = _json.loads(raw)
        except Exception:
            return

        if not info.get("visible"):
            return

        # reCAPTCHA checkbox is at the left of the iframe
        cx = info["x"] + 30
        cy = info["y"] + info["h"] / 2

        try:
            if hasattr(browser, "_cdp"):
                from cf_selenium import human_click
                browser._run(human_click(browser._cdp, cx, cy))
        except Exception:
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
