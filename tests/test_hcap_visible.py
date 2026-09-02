import os, time, json
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

    # Click submit to trigger hCaptcha
    b.execute_script(r"""(() => {
        const btn = document.querySelector('button[type=submit]') ||
                    Array.from(document.querySelectorAll('button')).find(b => b.textContent.toLowerCase().includes('claim'));
        if (btn) btn.click();
    })()""")
    time.sleep(4)

    # Force the challenge iframe to be visible + on top
    b.execute_script(r"""(() => {
        document.querySelectorAll('iframe[src*="hcaptcha.com"]').forEach(f => {
            if (f.src.includes('challenge')) {
                f.style.cssText = 'position: fixed !important; top: 50px !important; left: 200px !important; z-index: 999999 !important; width: 600px !important; height: 600px !important; display: block !important; border: 2px solid lime;';
            }
        });
    })()""")
    time.sleep(2)

    # Get final state of iframes
    all_iframes = b.execute_script(r"""(() => {
        return Array.from(document.querySelectorAll('iframe')).map(f => {
            const r = f.getBoundingClientRect();
            return {
                src: f.src.slice(0, 60),
                id: f.getAttribute('data-hcaptcha-widget-id'),
                x: r.x, y: r.y, w: r.width, h: r.height,
                visible: r.width > 0 && r.height > 0,
            };
        });
    })()""")
    print("All iframes after style fix:")
    print(json.dumps(all_iframes, indent=2))

    p = b.screenshot(full_page=True)
    print(f"\nFull-page shot: {p}")
