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

    # Click submit
    b.execute_script(r"""(() => {
        const btn = Array.from(document.querySelectorAll('button')).find(b =>
            b.textContent.toLowerCase().includes('claim') || b.type === 'submit');
        if (btn) btn.click();
    })()""")
    time.sleep(4)

    # Inspect hCaptcha widget divs
    info = b.execute_script(r"""(() => {
        const out = {
            hcaptchaDivs: [],
            hcaptchaIframes: [],
            allElementsWithHcaptcha: [],
        };
        // h-captcha divs
        document.querySelectorAll('div.h-captcha, [class*="h-captcha"], [data-hcaptcha-widget-id]').forEach(el => {
            const r = el.getBoundingClientRect();
            out.hcaptchaDivs.push({
                tag: el.tagName,
                cls: el.className.slice(0, 80),
                widgetId: el.getAttribute('data-hcaptcha-widget-id'),
                sitekey: el.getAttribute('data-sitekey'),
                x: r.x, y: r.y, w: r.width, h: r.height,
                visible: r.width > 0 && r.height > 0,
            });
        });
        // iframe
        document.querySelectorAll('iframe[src*="hcaptcha"]').forEach(f => {
            const r = f.getBoundingClientRect();
            out.hcaptchaIframes.push({
                src: f.src.slice(0, 100),
                id: f.id,
                dataHcaptchaWidgetId: f.getAttribute('data-hcaptcha-widget-id'),
                x: r.x, y: r.y, w: r.width, h: r.height,
            });
        });
        // Walk DOM
        document.querySelectorAll('*').forEach(el => {
            const attrs = el.attributes;
            for (const a of attrs) {
                if (a.name.includes('hcaptcha') || a.value.includes('hcaptcha')) {
                    out.allElementsWithHcaptcha.push({
                        tag: el.tagName,
                        name: a.name,
                        value: a.value.slice(0, 80),
                    });
                    break;
                }
            }
        });
        return out;
    })()""")
    print(json.dumps(info, indent=2))
