"""test_captcha_solver.py — Test the new hCaptcha/reCAPTCHA auto-solver adapters.

Runs against the mock server (127.0.0.1:18801) which doesn't have a real captcha
to solve, so we just verify:
  1. Detection still works (inherited from base adapters)
  2. The solver attempts to run (calls browser methods, waits for token)
  3. Falls back to HumanRequiredError if no token appears (expected in mock)

This is a smoke test — the real hCaptcha/reCAPTCHA solvers are exercised against
real challenges in production, not in this mock.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

# Add specter to path
sys.path.insert(0, str(Path.home() / "specter"))


def test_solver_registered():
    """Test that the solver adapters override the human-required ones."""
    from specter.providers import get_registry, ProviderId

    reg = get_registry()
    hc = reg.get(ProviderId.HCAPTCHA)
    rc = reg.get(ProviderId.RECAPTCHA)

    assert hc is not None, "hCaptcha adapter not registered"
    assert rc is not None, "reCAPTCHA adapter not registered"

    # The solver adapter should be the one registered (auto_solvable=True)
    assert hc.auto_solvable, f"hCaptcha not auto_solvable: {hc.__class__.__name__}"
    assert rc.auto_solvable, f"reCAPTCHA not auto_solvable: {rc.__class__.__name__}"
    assert hc.__class__.__name__ == "HCaptchaSolverAdapter"
    assert rc.__class__.__name__ == "ReCaptchaSolverAdapter"

    print(f"✅ hCaptcha: {hc.__class__.__name__} (auto_solvable={hc.auto_solvable})")
    print(f"✅ reCAPTCHA: {rc.__class__.__name__} (auto_solvable={rc.auto_solvable})")
    return True


def test_detection_unchanged():
    """Verify the solver adapter inherits detection logic from the base."""
    from specter.providers import (
        get_registry, ProviderId,
        HCaptchaAdapter, ReCaptchaAdapter,
        HCaptchaSolverAdapter, ReCaptchaSolverAdapter,
    )

    # Detection should be identical
    base_hc = HCaptchaAdapter()
    base_rc = ReCaptchaAdapter()
    sol_hc = HCaptchaSolverAdapter()
    sol_rc = ReCaptchaSolverAdapter()

    assert base_hc.BODY_MARKERS == sol_hc.BODY_MARKERS
    assert base_rc.BODY_MARKERS == sol_rc.BODY_MARKERS

    # Test signature_detect with mock hCaptcha body
    mock_body = '<html><body><div class="h-captcha" data-sitekey="abc-123"></div><script src="https://hcaptcha.com/1/api.js"></script></body></html>'
    headers = {}

    base_res = base_hc.signature_detect(headers, mock_body, "test.com")
    sol_res = sol_hc.signature_detect(headers, mock_body, "test.com")

    assert base_res.confidence == sol_res.confidence, \
        f"Detection confidence differs: base={base_res.confidence} sol={sol_res.confidence}"
    assert base_res.provider == sol_res.provider == ProviderId.HCAPTCHA
    assert sol_res.confidence >= 0.9, f"hCaptcha confidence too low: {sol_res.confidence}"

    # Test signature_detect with mock reCAPTCHA body
    mock_body_rc = '<html><body><div class="g-recaptcha" data-sitekey="6Lc..."></div><script src="https://www.google.com/recaptcha/api.js"></script></body></html>'
    base_res_rc = base_rc.signature_detect(headers, mock_body_rc, "test.com")
    sol_res_rc = sol_rc.signature_detect(headers, mock_body_rc, "test.com")

    assert base_res_rc.confidence == sol_res_rc.confidence
    assert sol_res_rc.provider == ProviderId.RECAPTCHA
    assert sol_res_rc.confidence >= 0.8, f"reCAPTCHA confidence too low: {sol_res_rc.confidence}"

    print(f"✅ hCaptcha detection: conf={sol_res.confidence:.2f} provider={sol_res.provider}")
    print(f"✅ reCAPTCHA detection: conf={sol_res_rc.confidence:.2f} provider={sol_res_rc.provider}")
    return True


def test_solver_signature():
    """Verify the solver can be invoked (will timeout on mock, that's expected)."""
    from specter.providers import HCaptchaSolverAdapter
    from specter.providers.base import HumanRequiredError

    # Mock browser that returns empty token
    class MockBrowser:
        url = "http://test.com/hcaptcha"
        def execute_script(self, js):
            return ""

        @property
        def cookies(self):
            return {}

    adapter = HCaptchaSolverAdapter()
    # Override timeout for test speed (pylance complains but it works at runtime)
    adapter.DEFAULT_TIMEOUT = 2  # type: ignore[misc]

    browser = MockBrowser()
    t0 = time.time()
    try:
        adapter.solve(browser, "http://test.com/hcaptcha")
        raise AssertionError("Expected HumanRequiredError")
    except HumanRequiredError as e:
        elapsed = time.time() - t0
        # Should timeout in ~2 seconds
        assert elapsed < 5, f"Solver took too long: {elapsed:.1f}s"
        assert e.provider == "hcaptcha"
        print(f"✅ Solver timeout: {int(elapsed)}s, raised HumanRequiredError as expected")
        return True


def main():
    print("=" * 60)
    print("CAPTCHA Solver Adapter Test")
    print("=" * 60)
    results = []
    for name, fn in [
        ("solver_registered", test_solver_registered),
        ("detection_unchanged", test_detection_unchanged),
        ("solver_signature", test_solver_signature),
    ]:
        try:
            ok = fn()
            results.append({"test": name, "ok": True})
            print(f"PASS: {name}\n")
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append({"test": name, "ok": False, "error": str(e)})
            print(f"FAIL: {name} — {e}\n")

    passed = sum(1 for r in results if r["ok"])
    print("=" * 60)
    print(f"Results: {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
