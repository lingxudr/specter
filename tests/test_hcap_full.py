import os, time
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

email = f"test{int(time.time())}@gmail.com"
pw = "TestPass123"

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    # Fill using BOTH native setter AND keystrokes (React-safe)
    js_fill = f"""(() => {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        const e = document.querySelector('input[type=email]');
        setter.call(e, '{email}');
        e.dispatchEvent(new Event('input', {{bubbles: true}}));
        e.dispatchEvent(new Event('change', {{bubbles: true}}));
        e.dispatchEvent(new Event('blur', {{bubbles: true}}));
        const p = document.querySelector('input[type=password]');
        setter.call(p, '{pw}');
        p.dispatchEvent(new Event('input', {{bubbles: true}}));
        p.dispatchEvent(new Event('change', {{bubbles: true}}));
        p.dispatchEvent(new Event('blur', {{bubbles: true}}));
        return JSON.stringify({{email: e.value, pw: p.value}});
    }})()"""
    print(f"Fill result: {b.execute_script(js_fill)}")
    time.sleep(1)

    # Check form state BEFORE submit
    state_before = b.execute_script(r"""(() => {
        const e = document.querySelector('input[type=email]');
        const p = document.querySelector('input[type=password]');
        return JSON.stringify({
            email_val: e?.value,
            email_required: e?.required,
            pw_val: p?.value,
            form_valid: e?.closest('form')?.checkValidity(),
        });
    })()""")
    print(f"Before submit: {state_before}")

    # Submit by clicking the actual visible button (not via text — find by type)
    b.execute_script(r"""(() => {
        const btn = document.querySelector('button[type=submit]') ||
                    Array.from(document.querySelectorAll('button')).find(b => b.textContent.toLowerCase().includes('claim'));
        if (btn) { btn.click(); return 'clicked: ' + btn.textContent.trim(); }
        return 'no btn';
    })()""")
    time.sleep(6)

    # Check form state + URL
    state_after = b.execute_script(r"""(() => {
        const e = document.querySelector('input[type=email]');
        const p = document.querySelector('input[type=password]');
        return JSON.stringify({
            url: location.href,
            email_val: e?.value,
            pw_val: p?.value,
            token: document.querySelector('[name=h-captcha-response]')?.value,
            token_len: (document.querySelector('[name=h-captcha-response]')?.value || '').length,
            errors: Array.from(document.querySelectorAll('[class*=error], [role=alert], [aria-invalid=true]')).map(e => e.textContent?.trim().slice(0, 80)),
        });
    })()""")
    print(f"After submit: {state_after}")

    # Look INSIDE the challenge iframe
    info = b.execute_script(r"""(() => {
        const iframes = Array.from(document.querySelectorAll('iframe[src*="hcaptcha.com"][src*="challenge"]'));
        return JSON.stringify({
            count: iframes.length,
            rects: iframes.map(f => JSON.parse(JSON.stringify(f.getBoundingClientRect()))),
            srcs: iframes.map(f => f.src.split('#')[1].slice(0, 60)),
        });
    })()""")
    print(f"Challenge iframes: {info}")

    # Screenshot showing form after submit
    path = b.screenshot()
    print(f"Screenshot: {path}")
