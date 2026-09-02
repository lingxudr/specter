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

    # Now call execute with the widget IDs
    result = b.execute_script(r"""(() => {
        return new Promise((resolve) => {
            const w = window.hcaptcha;
            if (!w) {resolve({error: 'no hcaptcha'}); return;}

            const widgets = w.getWidgets ? w.getWidgets() : [];
            const allFrames = document.querySelectorAll('iframe[data-hcaptcha-widget-id]');
            const widgetIds = Array.from(allFrames).map(f => f.getAttribute('data-hcaptcha-widget-id'));

            console.log('widgets:', widgets, 'iframeIds:', widgetIds);

            if (widgetIds.length === 0) {resolve({error: 'no widget ids', widgets}); return;}

            const wid = widgetIds[0];
            const t = setTimeout(() => {
                resolve({error: 'timeout', wid, currentResponse: w.getResponse(wid)});
            }, 30000);

            // Try execute
            try {
                const r = w.execute(wid, {async: false});
                if (r && r.then) {
                    r.then(token => {
                        clearTimeout(t);
                        resolve({success: true, wid, token, len: token?.length, resp: w.getResponse(wid)});
                    }).catch(e => {
                        clearTimeout(t);
                        resolve({error: 'execute err: ' + e.message, wid});
                    });
                } else {
                    clearTimeout(t);
                    resolve({success: true, wid, syncResult: r, resp: w.getResponse(wid)});
                }
            } catch (e) {
                clearTimeout(t);
                resolve({error: 'execute threw: ' + e.message, wid});
            }
        });
    })()""")
    print("execute() result:")
    print(json.dumps(result, indent=2))

    p = b.screenshot()
    print(f"\nShot: {p}")
