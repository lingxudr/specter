import os, time
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

email = f"test{int(time.time())}@gmail.com"
pw = "TestPass123"

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    js_fill = f"""(() => {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        const email_in = document.querySelector('input[type=email]');
        setter.call(email_in, '{email}');
        email_in.dispatchEvent(new Event('input', {{bubbles: true}}));
        const pw_in = document.querySelector('input[type=password]');
        setter.call(pw_in, '{pw}');
        pw_in.dispatchEvent(new Event('input', {{bubbles: true}}));
    }})()"""
    b.execute_script(js_fill)
    time.sleep(0.5)

    # Click submit
    b.execute_script(r"""(() => {
        const btns = Array.from(document.querySelectorAll('button'));
        for (const btn of btns) {
            const text = (btn.textContent || '').toLowerCase();
            if (text.includes('claim') || text.includes('sign up')) {
                btn.click();
                return;
            }
        }
    })()""")
    time.sleep(5)

    # Take a screenshot of just the hCaptcha area
    b.execute_script("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1)

    path = b.screenshot()
    print(f"Screenshot: {path}")

    # Get challenge info
    info = b.execute_script(r"""(() => {
        const iframes = Array.from(document.querySelectorAll('iframe'));
        return JSON.stringify({
            url: location.href,
            iframes: iframes.map(f => ({src: f.src.slice(0, 150), id: f.id, name: f.name})),
            challenge_visible: !!document.querySelector('.challenge-container, .hcap_challenge, [class*="challenge"]'),
            body_text: document.body.innerText.slice(0, 500),
        });
    })()""")
    print(f"Info: {info}")
