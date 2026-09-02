import os, time
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

email = f"test{int(time.time())}@gmail.com"
pw = "TestPass123"

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    # Fill
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

    # Submit
    b.execute_script(r"""(() => {
        const btn = document.querySelector('button[type=submit]') ||
                    Array.from(document.querySelectorAll('button')).find(b => b.textContent.toLowerCase().includes('claim'));
        if (btn) btn.click();
    })()""")
    time.sleep(5)

    # NOW: force hCaptcha challenge iframe on-screen
    moved = b.execute_script(r"""(() => {
        const iframes = Array.from(document.querySelectorAll('iframe[src*="hcaptcha.com"]'));
        let count = 0;
        iframes.forEach(f => {
            if (f.src.includes('challenge')) {
                // Override CSS to make visible
                f.style.cssText = 'position: fixed !important; top: 50px !important; left: 50% !important; transform: translateX(-50%) !important; z-index: 999999 !important; width: 600px !important; height: 600px !important; display: block !important; visibility: visible !important; opacity: 1 !important;';
                count++;
            }
        });
        return count;
    })()""")
    print(f"Moved {moved} challenge iframes on-screen")
    time.sleep(3)

    # Now look INSIDE the challenge iframe - this requires using CDP since it's cross-origin
    # Get iframe position for screenshot
    info = b.execute_script(r"""(() => {
        const ifr = Array.from(document.querySelectorAll('iframe[src*="hcaptcha.com"]')).find(f => f.src.includes('challenge'));
        if (!ifr) return 'no iframe';
        const r = ifr.getBoundingClientRect();
        return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height, src_id: ifr.src.split('id=')[1]?.split('&')[0]});
    })()""")
    print(f"Iframe pos: {info}")

    # Screenshot to see if challenge is now visible
    path = b.screenshot()
    print(f"Shot: {path}")

    # Check token after iframe is visible
    time.sleep(3)
    token = b.execute_script("document.querySelector('[name=h-captcha-response]')?.value || ''")
    print(f"Token after make-visible: '{token[:50]}' len={len(token)}")
