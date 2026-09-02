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

    # Now hCaptcha should be ready. Check its state
    state = b.execute_script(r"""(() => {
        const w = window.hcaptcha;
        if (!w) return {error: 'no hcaptcha'};
        const widgets = w.getWidgets ? w.getWidgets() : [];
        return {
            hasExecute: typeof w.execute === 'function',
            hasGetResponse: typeof w.getResponse === 'function',
            widgets: widgets.map((wid, i) => ({
                id: wid,
                response: w.getResponse(wid),
                respKey: w.getRespKey ? w.getRespKey(wid) : null,
            })),
        };
    })()""")
    print("hCaptcha state after submit:")
    print(json.dumps(state, indent=2))

    # Now try to call execute() and wait for response
    print("\n=== Trying hcaptcha.execute() ===")
    result = b.execute_script(r"""(() => {
        return new Promise((resolve) => {
            const w = window.hcaptcha;
            if (!w || !w.execute) {resolve({error: 'no execute'}); return;}

            const widgets = w.getWidgets ? w.getWidgets() : [];
            if (widgets.length === 0) {resolve({error: 'no widgets'}); return;}

            // Listen for the response event
            let captured = null;
            const orig = document.querySelector('[data-hcaptcha-widget-id]')?.outerHTML;
            console.log('Calling hcaptcha.execute on widget:', widgets[0]);

            // Set timeout
            const t = setTimeout(() => {
                resolve({error: 'timeout 30s', captured, origSnippet: orig?.slice(0, 200)});
            }, 30000);

            // Poll for response after execute
            w.execute(widgets[0], {async: false}).then((res) => {
                clearTimeout(t);
                resolve({success: true, response: res, responseFromGet: w.getResponse(widgets[0])});
            }).catch((e) => {
                clearTimeout(t);
                resolve({error: 'execute promise rejected: ' + e.message, captured});
            });
        });
    })()""")
    print("Execute result:")
    print(json.dumps(result, indent=2))

    # Take screenshot
    p = b.screenshot()
    print(f"\nShot: {p}")
