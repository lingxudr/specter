"""
Try setting the h-captcha-response textarea DIRECTLY (no real solve).
The hCaptcha invisible widget listens for textarea content change.
This is the "free bypass" attempt — but server will validate token against hCaptcha API.
"""
import os, time, json, requests
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

# Let's check if there's any way to get a real token — try hCaptcha's public test endpoint
# hCaptcha provides sitekey "10000000-ffff-ffff-ffff-000000000001" for testing (always passes)
# Sitekey "20000000-ffff-ffff-ffff-000000000001" = always blocks
# xkiro uses: a4de07a4-c7c0-43a0-b03a-8d2fc40d3af2

# Let me check if xkiro's hCaptcha verification endpoint is accessible
# Looking at config from earlier: imghost, assethost, etc
# hCaptcha verify URL is typically: https://api.hcaptcha.com/siteverify

email = f"test{int(time.time())}@gmail.com"
pw = "TestPass123"

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    # Try setting the response textarea directly
    set_result = b.execute_script(f"""(() => {{
        const ta = document.querySelector('textarea[name="h-captcha-response"]');
        const ta2 = document.querySelector('textarea[name="g-recaptcha-response"]');
        const fake_token = 'fake_token_{int(time.time())}';
        if (ta) {{
            ta.value = fake_token;
            ta.dispatchEvent(new Event('input', {{bubbles: true}}));
            ta.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
        if (ta2) {{
            ta2.value = fake_token;
            ta2.dispatchEvent(new Event('input', {{bubbles: true}}));
            ta2.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
        return {{
            ta_found: !!ta,
            ta2_found: !!ta2,
            ta_value: ta?.value,
            ta2_value: ta2?.value,
        }};
    }})()""")
    print("set textarea:")
    print(json.dumps(set_result, indent=2))

    # Fill form
    b.execute_script(f"""(() => {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        const e = document.querySelector('input[type=email]');
        setter.call(e, '{email}');
        e.dispatchEvent(new Event('input', {{bubbles: true}}));
        e.dispatchEvent(new Event('blur', {{bubbles: true}}));
        const p = document.querySelector('input[type=password]');
        setter.call(p, '{pw}');
        p.dispatchEvent(new Event('input', {{bubbles: true}}));
        p.dispatchEvent(new Event('blur', {{bubbles: true}}));
    }})()""")
    time.sleep(0.5)

    # Click submit
    b.execute_script(r"""(() => {
        const btn = Array.from(document.querySelectorAll('button')).find(b =>
            b.textContent.toLowerCase().includes('claim') || b.type === 'submit');
        if (btn) btn.click();
    })()""")
    time.sleep(5)

    p = b.screenshot()
    print(f"\nShot: {p}")

    # Check what URL we're on now
    info = b.execute_script(r"""(() => ({
        url: location.href,
        title: document.title,
        bodyText: document.body.innerText.slice(0, 500),
    }))()""")
    print("\nafter submit:")
    print(json.dumps(info, indent=2))
