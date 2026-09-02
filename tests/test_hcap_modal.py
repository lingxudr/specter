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
    js_fill = f"""(() => {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        const e = document.querySelector('input[type=email]');
        setter.call(e, '{email}');
        e.dispatchEvent(new Event('input', {{bubbles: true}}));
        const p = document.querySelector('input[type=password]');
        setter.call(p, '{pw}');
        p.dispatchEvent(new Event('input', {{bubbles: true}}));
    }})()"""
    b.execute_script(js_fill)
    time.sleep(0.5)

    # Submit
    b.execute_script(r"""(() => {
        for (const b of document.querySelectorAll('button')) {
            if (b.textContent.toLowerCase().includes('claim')) { b.click(); return; }
        }
    })()""")
    time.sleep(5)

    # Scroll inside the modal to bottom, then screenshot
    b.execute_script(r"""(() => {
        // Find scrollable modal
        const modal = document.querySelector('[role=dialog], .modal, [class*=modal]') || document.scrollingElement;
        modal.scrollTop = modal.scrollHeight;
        // Also scroll any inner scrollable
        document.querySelectorAll('*').forEach(el => {
            if (el.scrollHeight > el.clientHeight && getComputedStyle(el).overflow !== 'visible') {
                el.scrollTop = el.scrollHeight;
            }
        });
    })()""")
    time.sleep(2)
    path = b.screenshot(full_page=True)
    print(f"Full page: {path}")

    # Also screenshot the specific hcaptcha iframe
    b.execute_script("document.querySelectorAll('iframe').forEach(f => f.scrollIntoView({block: 'center'}))")
    time.sleep(1)
    path2 = b.screenshot()
    print(f"After scroll: {path2}")

    # Get hcaptcha challenge state
    info = b.execute_script(r"""(() => {
        const iframes = Array.from(document.querySelectorAll('iframe[src*="hcaptcha.com"]'));
        return JSON.stringify({
            url: location.href,
            hcaptcha_iframes: iframes.map(f => f.src),
            hcaptcha_token_response: document.querySelector('[name=h-captcha-response]')?.value || 'none',
            iframe_responses: Array.from(document.querySelectorAll('iframe[name^="hcaptcha"], iframe[title*="aptcha"]')).map(f => ({name: f.name, title: f.title, src: f.src.slice(0, 100)})),
        });
    })()""")
    print(f"Info: {info}")
