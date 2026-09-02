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

    # Deep inspect the hCaptcha widget
    info = b.execute_script(r"""(() => {
        const allFrames = document.querySelectorAll('iframe[data-hcaptcha-widget-id]');
        const wid = allFrames[0].getAttribute('data-hcaptcha-widget-id');
        const w = window.hcaptcha;
        const widgetContainer = allFrames[0].parentElement;
        const widgetParent = widgetContainer?.parentElement;
        const allParents = [];
        let el = widgetContainer;
        while (el && el !== document.body) {
            const r = el.getBoundingClientRect();
            allParents.push({
                tag: el.tagName,
                cls: (el.className || '').toString().slice(0, 100),
                id: el.id,
                x: r.x, y: r.y, w: r.width, h: r.height,
                children: el.children.length,
            });
            el = el.parentElement;
        }
        return {
            widgetId: wid,
            widgetContainerOuterHTML: widgetContainer?.outerHTML.slice(0, 600),
            widgetContainerParentOuterHTML: widgetParent?.outerHTML.slice(0, 400),
            allParents: allParents.slice(0, 5),
            response: w.getResponse(wid),
            responseLength: w.getResponse(wid)?.length,
        };
    })()""")
    print(json.dumps(info, indent=2))

    p = b.screenshot()
    print(f"\nShot: {p}")
