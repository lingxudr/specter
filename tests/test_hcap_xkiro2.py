import os, time, json
os.chdir(os.path.expanduser('~/specter'))
import sys; sys.path.insert(0, '.')
from cf_selenium import Browser

email = f"test{int(time.time())}@gmail.com"
pw = "TestPass123"

with Browser(headless=True) as b:
    b.get('https://xkiro.com/register')
    time.sleep(3)

    # Hook hcaptcha BEFORE form interaction
    b.execute_script(r"""
        window.__hcaptcha_calls = [];
        if (window.hcaptcha) {
            const origExec = window.hcaptcha.execute;
            window.hcaptcha.execute = function(...args) {
                window.__hcaptcha_calls.push({method: 'execute', t: Date.now()});
                return origExec.apply(this, args);
            };
        }
    """)

    # Fill form via React-compatible setter
    js_fill = f"""(() => {{
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        const email_in = document.querySelector('input[type=email]');
        setter.call(email_in, '{email}');
        email_in.dispatchEvent(new Event('input', {{bubbles: true}}));
        const pw_in = document.querySelector('input[type=password]');
        setter.call(pw_in, '{pw}');
        pw_in.dispatchEvent(new Event('input', {{bubbles: true}}));
        return JSON.stringify({{email: email_in.value, pw: pw_in.value, emailLen: email_in.value.length}});
    }})()"""
    filled = b.execute_script(js_fill)
    print(f"Filled: {filled}")

    time.sleep(1)
    calls = b.execute_script("JSON.stringify(window.__hcaptcha_calls || [])")
    print(f"Calls before submit: {calls}")

    # Click submit
    submit_clicked = b.execute_script(r"""(() => {
        const btns = Array.from(document.querySelectorAll('button'));
        for (const btn of btns) {
            const text = (btn.textContent || '').toLowerCase();
            if (text.includes('claim') || text.includes('sign up')) {
                btn.click();
                return 'clicked: ' + text;
            }
        }
        return 'no button';
    })()""")
    print(f"Submit: {submit_clicked}")

    time.sleep(6)

    state = b.execute_script(r"""(() => {
        return JSON.stringify({
            calls: window.__hcaptcha_calls,
            iframes: Array.from(document.querySelectorAll('iframe')).map(f => f.src.slice(0, 100)),
            hcap_div: !!document.querySelector('.h-captcha'),
            sitekey_elems: Array.from(document.querySelectorAll('[data-sitekey]')).map(e => e.getAttribute('data-sitekey')),
            url: location.href,
            hcaptcha_response: document.querySelector('[name=h-captcha-response], [name=g-recaptcha-response]')?.value?.slice(0, 50) || 'none',
        });
    })()""")
    print(f"State after submit: {state}")
