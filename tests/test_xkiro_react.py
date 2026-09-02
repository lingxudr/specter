#!/usr/bin/env python3
"""Test React-compatible type/clear against real xkiro.com register form."""
import asyncio
import json
import sys
import time
sys.path.insert(0, "/data/data/com.termux/files/home/specter")

from cf_selenium import Browser

TARGET = "https://xkiro.com/register"

async def main():
    b = Browser(
        headless=True,
        profile="xkiro_react_test",
        auto_solve=True,
    )
    try:
        await b.start()
        print("[1/6] Chrome started")
        # warm up
        await b._cdp.cmd("Page.enable", {}, timeout=5)

        print(f"[2/6] Navigating to {TARGET}")
        await b.goto(TARGET, wait_for="body")
        await asyncio.sleep(2)

        # Inject anti-bot detection
        await b._cdp.js("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}, csi: () => {}, loadTimes: () => {}};
        """, timeout=5)

        # Detect hCaptcha sitekey
        sk = await b._cdp.js("""
            (() => {
              const m = document.body.innerHTML.match(/sitekey["']?\s*[:=]\s*["']([0-9a-f-]{36})/i);
              return m ? m[1] : null;
            })()
        """, timeout=5)
        print(f"[3/6] hCaptcha sitekey: {sk}")

        # Find inputs
        info = await b._cdp.js("""
            (() => {
              const inputs = Array.from(document.querySelectorAll('input, textarea'));
              return inputs.map((e, i) => ({
                i, tag: e.tagName, type: e.type || '',
                name: e.name || '', id: e.id || '',
                placeholder: e.placeholder || '',
                ariaLabel: e.getAttribute('aria-label') || '',
                autocomplete: e.autocomplete || '',
                visible: !!(e.offsetWidth || e.offsetHeight),
              }));
            })()
        """, timeout=5)
        print(f"[4/6] Inputs found: {json.dumps(info, indent=2)}")

        # Test fill using type() which now uses React setter
        test_email = f"testreact{int(time.time())}@gmail.com"
        print(f"[5/6] Testing type() with email: {test_email}")
        try:
            # Find email input - use a robust selector
            await b._cdp.js(f"""
                (() => {{
                  const inputs = document.querySelectorAll('input[type="email"]');
                  if (inputs.length) {{
                    inputs[0].scrollIntoView({{block: 'center'}});
                    inputs[0].focus();
                  }}
                }})()
            """, timeout=5)
            await asyncio.sleep(0.3)
            el = b.find_element('input[type="email"]')
            await el.type(test_email, human=True)
            await asyncio.sleep(0.5)
            val = await b._cdp.js("""
                document.querySelector('input[type="email"]').value
            """, timeout=5)
            print(f"  typed value: {val!r}  match={val == test_email}")
        except Exception as e:
            print(f"  type() error: {e}")

        # Snapshot form state
        snap = await b._cdp.js("""
            (() => {
              const data = {};
              const inputs = document.querySelectorAll('input, textarea');
              inputs.forEach((e, i) => {
                data[`input_${i}_${e.type||e.tagName}_${e.name||e.id||e.placeholder.slice(0,15)}`]
                  = e.value;
              });
              return data;
            })()
        """, timeout=5)
        print(f"[6/6] Form state after fill:")
        for k, v in (snap or {}).items():
            print(f"    {k}: {v!r}")

        # Check React validation state
        react_state = await b._cdp.js("""
            (() => {
              const email = document.querySelector('input[type="email"]');
              if (!email) return null;
              // Get the React fiber to check internal value
              const reactKey = Object.keys(email).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
              if (!reactKey) return {has_fiber: false};
              // Check error/validation state
              const errs = document.querySelectorAll('[role="alert"], .text-red-500, .text-destructive, [class*="error"]');
              return {
                has_fiber: true,
                visible_errors: Array.from(errs).map(e => e.textContent.trim()).slice(0, 5),
                dom_value: email.value,
                classList: email.className,
              };
            })()
        """, timeout=5)
        print(f"React state: {json.dumps(react_state, indent=2)}")

        # Take screenshot
        await b.screenshot(path="/data/data/com.termux/files/home/.cache/xkiro_react_test.png")
        print("\nScreenshot: /data/data/com.termux/files/home/.cache/xkiro_react_test.png")

    finally:
        await b.close()

asyncio.run(main())
