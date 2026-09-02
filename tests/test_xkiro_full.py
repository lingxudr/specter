#!/usr/bin/env python3
"""Full SPECTER test against xkiro.com — Sign up with hCaptcha auto-solve.

Strategy:
  1. Load /register page
  2. Find the Sign UP form (has email + password + referral)
  3. Fill email/password into THAT form only
  4. Wait for hCaptcha to render
  5. Run SPECTER hCaptcha solver (multi-strategy)
  6. Click the Sign up submit button
  7. Verify success (redirect to dashboard or token response)
"""
import sys, os, time, json, traceback
sys.path.insert(0, os.path.expanduser("~/specter"))
sys.path.insert(0, os.path.expanduser("~/specter/specter"))

os.chdir(os.path.expanduser("~/specter"))

from cf_selenium import Browser

EMAIL = f"test{int(time.time())}@gmail.com"
PASSWORD = "TestPassword123!"

def js(b, expr):
    """Run JS, return parsed JSON if possible, else raw string."""
    out = b.execute_script(expr)
    if isinstance(out, str):
        try:
            return json.loads(out)
        except:
            return out
    return out

def main():
    print("=" * 60)
    print("SPECTER Full Test vs xkiro.com (Sign up + hCaptcha)")
    print("=" * 60)

    with Browser(headless=True) as b:
        b.get("https://xkiro.com/register")
        time.sleep(2)

        # ─── 1. Find Sign UP form ─────────────────────────────────────
        print("\n[1] Form structure...")
        forms = js(b, """(() => {
            return Array.from(document.querySelectorAll('form')).map((f, i) => {
                const submit = f.querySelector('button[type="submit"]');
                return {
                    idx: i,
                    submit: submit?.textContent?.trim().slice(0, 30) || '',
                    has_email: !!f.querySelector('input[type="email"]'),
                    has_pw: !!f.querySelector('input[type="password"]'),
                    has_referral: !!f.querySelector('input[name="referralCode"]'),
                    inputs: Array.from(f.querySelectorAll('input')).map(x => `${x.type}:${x.name||x.placeholder||'?'}`).join(',')
                };
            });
        })()""")
        su_idx = None
        for f in (forms or []):
            print(f"  Form {f['idx']}: submit='{f['submit']}' email={f['has_email']} pw={f['has_pw']} referral={f['has_referral']}")
            if su_idx is None and f.get('has_email') and f.get('has_pw') and f.get('has_referral'):
                su_idx = f['idx']
        if su_idx is None:
            print("  ✗ No Sign up form found")
            return False
        print(f"  ✓ Sign up form is index {su_idx}")

        # ─── 2. Fill the Sign up form ─────────────────────────────────
        print(f"\n[2] Filling Sign up form (email={EMAIL})...")
        fill_result = js(b, f"""(() => {{
            const form = document.querySelectorAll('form')[{su_idx}];
            if (!form) return JSON.stringify({{error: 'form not found'}});
            const emailEl = form.querySelector('input[type="email"]');
            const pwEl = form.querySelector('input[type="password"]');
            const setVal = (el, val) => {{
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }};
            setVal(emailEl, '{EMAIL}');
            setVal(pwEl, '{PASSWORD}');
            return JSON.stringify({{
                email: emailEl.value,
                pw: pwEl.value ? pwEl.value.length + ' chars' : ''
            }});
        }})()""")
        print(f"  Fill result: {fill_result}")

        time.sleep(0.5)
        post_state = js(b, f"""(() => {{
            const form = document.querySelectorAll('form')[{su_idx}];
            const email = form.querySelector('input[type="email"]');
            const pw = form.querySelector('input[type="password"]');
            return JSON.stringify({{email: email?.value, pw_len: pw?.value?.length || 0}});
        }})()""")
        print(f"  Post-fill state: {post_state}")

        # ─── 3. Wait for hCaptcha to render ───────────────────────────
        print("\n[3] Waiting for hCaptcha to render...")
        for i in range(15):
            hc = js(b, """(() => ({
                iframe: !!document.querySelector('iframe[src*="hcaptcha"]'),
                div: !!document.querySelector('[data-hcaptcha-widget-id], div.h-captcha'),
                response_input: !!document.querySelector('input[name="h-captcha-response"]')
            }))()""")
            print(f"  t={i*0.5:.1f}s: {hc}")
            if isinstance(hc, dict) and hc.get('iframe'):
                break
            time.sleep(0.5)

        # ─── 4. Run SPECTER hCaptcha solver ───────────────────────────
        print("\n[4] Running SPECTER hCaptcha solver...")
        t0 = time.time()
        try:
            from specter.providers.hcaptcha_solver_adapter import HCaptchaSolverAdapter
            adapter = HCaptchaSolverAdapter()
            print(f"  Adapter: {type(adapter).__name__}, auto_solvable={adapter.auto_solvable}")
            result = adapter.solve(b, b.url)
            print(f"  Solver: {result} (in {time.time()-t0:.1f}s)")
        except Exception as e:
            print(f"  Solver error: {e}")
            traceback.print_exc()
            result = {"success": False, "error": str(e)}

        # ─── 5. Get token (if any) ────────────────────────────────────
        print("\n[5] Token check after solver...")
        token = js(b, """(() => {
            const el = document.querySelector('input[name="h-captcha-response"]');
            return el ? (el.value || '').slice(0, 50) : 'no input';
        })()""")
        print(f"  h-captcha-response: {token!r}")

        # ─── 6. Click Sign up submit ──────────────────────────────────
        print("\n[6] Clicking Sign up submit...")
        click_result = js(b, f"""(() => {{
            const form = document.querySelectorAll('form')[{su_idx}];
            const btn = form?.querySelector('button[type="submit"]');
            if (!btn) return JSON.stringify({{error: 'no submit button in form'}});
            btn.click();
            return JSON.stringify({{clicked: true, text: btn.textContent.trim().slice(0, 30)}});
        }})()""")
        print(f"  Click: {click_result}")
        time.sleep(3)

        # ─── 7. Check result ──────────────────────────────────────────
        print("\n[7] Result...")
        final = js(b, """(() => ({
            url: location.href,
            title: document.title,
            body: document.body.textContent.slice(0, 200),
            hcap_input: document.querySelector('input[name="h-captcha-response"]')?.value?.slice(0, 30) || 'none'
        }))()""")
        print(f"  URL: {final.get('url') if isinstance(final, dict) else final}")
        print(f"  Title: {final.get('title') if isinstance(final, dict) else 'n/a'}")
        if isinstance(final, dict):
            print(f"  Body[:200]: {str(final.get('body'))[:200]!r}")
            print(f"  Token: {final.get('hcap_input')}")
            url = str(final.get('url', ''))
            body = str(final.get('body', ''))
            if "dashboard" in url.lower() or "success" in body.lower():
                print("\n  ✅ SUCCESS! Registration likely completed.")
                return True
        print("\n  ❌ Did not reach success page.")
        return False

if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
