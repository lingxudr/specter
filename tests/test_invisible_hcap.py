#!/usr/bin/env python3
"""Test invisible hCaptcha on xkiro.com with clean mobile fingerprint.
hCaptcha invisible mode should auto-solve if fingerprint passes."""
import time
from cf_selenium import Browser

email = f"test{int(time.time())}@gmail.com"
print(f"Email: {email}")
print("Loading xkiro.com/register with fresh mobile fingerprint...")

with Browser(headless=True, profile="incognito") as b:
    b.get("https://xkiro.com/register")
    time.sleep(3)

    # Snapshot initial state
    state = b.execute_script("""
        ({
            hasHcaptcha: !!window.hcaptcha,
            hasWidget: !!document.querySelector('div.h-captcha, [data-hcaptcha-widget-id]'),
            iframes: Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: (f.src || '').slice(0, 120),
                visible: f.offsetWidth > 0 && f.offsetHeight > 0
            })).filter(x => x.src.includes('hcaptcha') || x.src.includes('challenges'))
        })
    """)
    print(f"\n[1] Initial state:")
    print(f"   hCaptcha JS loaded: {state['hasHcaptcha']}")
    print(f"   Widget visible: {state['hasWidget']}")
    print(f"   hCaptcha iframes: {len(state['iframes'])}")
    for ifr in state['iframes']:
        print(f"     - {ifr['src'][:100]}... visible={ifr['visible']}")

    # Fill form fields
    print(f"\n[2] Filling form...")
    inputs = b.find_elements("input:not([type='hidden'])")
    for inp in inputs:
        itype = inp.get_attr("type")
        if itype == "email":
            inp.type(email)
            print(f"   Email: {email}")
        elif itype == "password":
            inp.type("TestPass123!")
            print(f"   Password: set")
    time.sleep(1)

    # Click submit
    print(f"\n[3] Clicking submit...")
    btns = b.find_elements("button")
    submit = None
    for btn in btns:
        txt = btn.text().lower() if hasattr(btn, 'text') else ""
        if "sign up" in txt or "register" in txt or "create" in txt:
            submit = btn
            break
    if submit:
        submit.click(human=True)
    else:
        b.execute_script("document.querySelector('form').requestSubmit()")
    time.sleep(8)

    # Final state
    final = b.execute_script("""
        ({
            url: location.href,
            title: document.title,
            bodyText: document.body.innerText.slice(0, 500),
            hcapResponse: window.hcaptcha && window.hcaptcha.getResponse ? window.hcaptcha.getResponse() : null,
            hcapWidgetCount: window.hcaptcha && window.hcaptcha.getWidgets ? window.hcaptcha.getWidgets().length : 0,
            formAction: document.querySelector('form')?.action,
            formMethod: document.querySelector('form')?.method,
        })
    """)
    print(f"\n[4] Final state:")
    print(f"   URL: {final['url']}")
    print(f"   Title: {final['title']}")
    print(f"   hCaptcha response: {final['hcapResponse']}")
    print(f"   hCaptcha widgets: {final['hcapWidgetCount']}")
    print(f"   Body text (first 500):")
    print(f"   {final['bodyText']}")

    b.screenshot("/data/data/com.termux/files/home/test_invisible_hcap.png")
    print(f"\n[5] Screenshot: /data/data/com.termux/files/home/test_invisible_hcap.png")
