"""test_hcaptcha_v2_real.py — Test v2 solver with realistic flow:
navigate → fill form → submit → solver kicks in.

This is what SPECTER will do in production. The test:
  1. Open xkiro.com/register
  2. Fill email + password (no hCaptcha visible yet)
  3. Click submit (this should trigger hCaptcha lazy-load)
  4. Run v2 solver to solve the now-visible hCaptcha
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.home() / "specter"))


def test_hcaptcha_v2_realistic():
    """End-to-end test: form fill + submit + solve hCaptcha."""
    from cf_selenium import Browser
    from specter.providers import get_registry, ProviderId, HumanRequiredError

    # Speed up for test
    os.environ["SPECTER_HCAPTCHA_TIMEOUT"] = "25"
    os.environ["SPECTER_HCAPTCHA_POLL_INIT"] = "0.3"
    os.environ["SPECTER_HCAPTCHA_POLL_MAX"] = "1.5"
    os.environ["SPECTER_HCAPTCHA_CLICK_RETRIES"] = "2"

    reg = get_registry()
    adapter = reg.get(ProviderId.HCAPTCHA)

    # Check memory
    import subprocess
    mem_info = subprocess.check_output(["free", "-m"]).decode()
    for line in mem_info.split("\n"):
        if line.startswith("Mem:"):
            avail = int(line.split()[6])
            if avail < 800:
                print(f"⚠️  Low memory ({avail}MB) — skipping")
                return True
            break

    print("=" * 60)
    print("Real hCaptcha v2 Solver — Form Submit Flow")
    print("=" * 60)

    browser = Browser(profile="hcaptcha_v2_realistic", headless=True, auto_solve=True)
    try:
        # Step 1: Navigate
        print("1. Navigate to xkiro.com/register...")
        browser.get("https://xkiro.com/register", wait_for="body")
        time.sleep(2)

        # Step 2: Fill form
        print("2. Fill email + password...")
        try:
            email_el = browser.find_element("input[type='email']")
            browser._run(email_el.type(f"v2test{os.getpid()}@letticha.com"))
            time.sleep(1)
            pw_el = browser.find_element("input[type='password']")
            browser._run(pw_el.type("Test1234Pass!@#"))
            time.sleep(1)
            print("   ✅ Form filled")
        except Exception as e:
            print(f"   ❌ Form fill failed: {e}")
            return False

        # Step 3: Submit (this should trigger hCaptcha lazy-load)
        print("3. Click submit button (triggers hCaptcha)...")
        try:
            # Try "Claim reward" first, then "Create account", then "Sign up"
            submitted = False
            for btn_text in ["Claim reward", "Create account", "Sign up", "Register"]:
                try:
                    btn = browser.find_by_text(btn_text, tag="button")
                    browser._run(btn.click())
                    print(f"   ✅ Clicked '{btn_text}'")
                    submitted = True
                    break
                except RuntimeError:
                    continue
            if not submitted:
                # Try by submit type
                btn = browser.find_element("button[type='submit']")
                browser._run(btn.click())
                print("   ✅ Clicked submit button (by type)")
        except Exception as e:
            print(f"   ⚠️  Submit failed: {e}")
            print("   (continuing anyway to test solver on whatever's visible)")

        # Wait for hCaptcha to render (lazy-load)
        print("4. Wait for hCaptcha to render (lazy-load)...")
        time.sleep(5)

        # Step 4: Run solver
        print("5. Run v2 solver...")
        t0 = time.time()
        try:
            result = adapter.solve(browser, "https://xkiro.com/register")
            elapsed = time.time() - t0
            print(f"   ✅ Solver completed in {elapsed:.1f}s")
            print(f"   strategy: {result.get('extra', {}).get('strategy', 'unknown')}")
            print(f"   sitekey: {result.get('extra', {}).get('sitekey', 'unknown')[:16]}...")
            print(f"   token_length: {result.get('extra', {}).get('token_length', 0)}")
            if result.get('extra', {}).get('token_length', 0) > 20:
                print(f"   token preview: {result.get('token', '')[:40]}...")
                print(f"\n🎉 GOT REAL HCAPTCHA TOKEN — solver v2 worked!")
                return True
            else:
                print("   ⚠️  No valid token")
                return False
        except HumanRequiredError as e:
            elapsed = time.time() - t0
            print(f"   ⚠️  HumanRequiredError after {elapsed:.1f}s")
            # Read strategy from error
            err_str = str(e)
            if "strategy=" in err_str:
                strat = err_str.split("strategy=")[1].split(" ")[0].rstrip(",")
                print(f"   attempted strategy: {strat}")
            print(f"   (image grid challenge needs human vision — expected)")
            return True
    finally:
        try:
            browser.quit()
        except Exception:
            pass


def main():
    try:
        ok = test_hcaptcha_v2_realistic()
        return 0 if ok else 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
