# SPECTER

> **S**taging **P**rotection **C**lassification, **E**scalation & **T**oken-lifecycle **E**ngine for **R**esearch

A multi-provider AI web-agent that **observes, classifies, and reasons** about anti-bot protection systems (Cloudflare, Akamai, DataDome, Imperva, AWS WAF, reCAPTCHA, hCaptcha, Arkose) on **authorized staging environments** — without auto-solving challenges or fabricating tokens.

> ⚠️ **SPECTER is intentionally incomplete.** It will *never* bypass a challenge, generate a token, or solve a CAPTCHA on your behalf. The token-lifecycle machinery exists to manage tokens you obtained legitimately (out-of-band). The vision layer is **decision-only** — it observes pages and advises the planner; it does not click, type, or submit.

---

## Why this exists

Most anti-bot toolkits are black boxes: they hide what they detect, claim magic, and rely on commercial solver APIs. SPECTER is the opposite — every detection, every state transition, every token read/write is observable, inspectable, and reversible. It is built for security engineers who need to:

- **Audit their own staging site** to find which protections fire on which pages
- **Stress-test the gap** between a fresh browser and a hardened real one
- **Manage AWS WAF / Cloudflare token lifecycles** without writing throwaway shell scripts
- **Compare DOM signals vs OCR vs LLM-based vision** when classifying a challenge

---

## What you get

| Component | Purpose | Path |
|---|---|---|
| **Agent** | snapshot → decide → act loop with trace + screenshots | `specter/agent.py` |
| **Provider abstraction** | 8 named providers + `unknown` sentinel | `specter/providers/` |
| **Vision layer** | Tesseract (local) + Claude (paid) + DOM (free) | `specter/vision/` |
| **Session layer** | multi-provider BypassSession with namespace `provider:host` | `specter/sessions.py` |
| **Token store** | persistent JSON lifecycle for AWS WAF / CF tokens | `specter/providers/aws_waf_token.py` |
| **Tool layer** | extract / plan / browse / session with bypass-flag gating | `specter/tools.py` |
| **Mock server** | stdlib HTTP server, 20 endpoints, runs anywhere | `specter/mock_server.py` |
| **Test suite** | 30+ tests across 6 files, runs on 6GB Android | `tests/` |

---

## Installation

```bash
# 1. clone
git clone https://github.com/letticha/specter.git
cd specter

# 2. frozen dependencies (Selenium-style browser + persistent session DB)
# These are siblings of the specter/ package. They are FROZEN and intentionally
# not part of the import graph — you drop them next to the package and import
# them by name.
#   specter/cf_selenium.py     # browser control + CF bypass
#   specter/cf_persistent.py   # SQLite session/cookie store
# In this repo they are vendored as siblings. See "Frozen modules" below.

# 3. Python deps (minimal — only Pillow + requests, and only if you use vision/real sites)
pip install pillow requests

# 4. Tesseract (only for vision/OCR — apt on Termux/Debian/Ubuntu)
apt install tesseract-ocr   # or: pkg install tesseract (Termux)

# 5. Chromium (only if you run real-browser tests; mock tests work headless)
pkg install chromium         # Termux
# OR
apt install chromium-browser # Debian/Ubuntu
```

### Frozen modules

`cf_selenium.py` and `cf_persistent.py` are **NOT** part of this project's import graph. They live as siblings of the `specter/` package and provide the browser/Selenium-style control plane + the SQLite session store. We vendor them locally and treat their public API as a contract — you can replace them with any equivalent (e.g. plain Playwright + a different DB) and the rest of the project works unchanged.

**Do not modify them.** If you need new behavior, add it to the `specter/` package and the providers in `specter/providers/`.

---

## Quick start

```python
from specter import AIWebAgent

with AIWebAgent(
    profile="my-staging",
    allowed_domains=["staging.example.com", "127.0.0.1"],
    authorized_test_mode=True,
    use_llm=True,       # uses routerku on http://127.0.0.1:20130 (or set HERMES_CUSTOM_127_0_0_1_20130_API_KEY)
    use_vision=True,    # decision-only; needs `tesseract` on PATH
) as agent:
    result = agent.run("https://staging.example.com/dashboard", goal="describe the page")
    print(result.success, result.summary, result.artifacts_dir)
```

### CLI

```bash
# observational goal (no LLM, no vision)
python -m specter "https://staging.example.com" "what is the title?" \
    --allow staging.example.com --no-llm --max-steps 2

# full agent loop
python -m specter "https://staging.example.com" "click checkout, fill form" \
    --allow staging.example.com --max-steps 8 --use-vision

# task JSON
python -m specter --task task.json

# start the bundled mock staging server (20 endpoints on 127.0.0.1:18801)
python -m specter --serve-mock --port 18801
```

### Run the test suite

```bash
# 1. start the mock server (it stays up; multiple test files share it)
python -m specter --serve-mock &

# 2. run individual test files (each is a standalone script with TestContext)
python3 tests/test_aws_waf_token.py        # 8/8 — token lifecycle (no browser)
python3 tests/test_vision.py               # 3/3 — vision layer (subprocess per test)
python3 tests/test_cf_agent_vision.py      # 3/3 — vision + agent integration
python3 tests/test_prod_style.py           # 9/9 — production-style full agent
python3 tests/test_real_sites.py           # 6/6 — real public sites (no protection)
python3 tests/test_e2e_integration.py      # 4/4 — vision + multi-provider + AWS WAF + session
```

Expected runtime on a 6GB Android (Termux): ~6–7 minutes total. Memory ceiling: ~1.2GB free before each Chrome subtest (run a `pkill -9 chrome && sleep 2` between sessions if you're tight).

Artifacts per test land in `~/test_logs/<test_name>/<NN_scenario>/{result.json,log.txt,*.db,tokens.json,screenshots/}` plus a `summary.json` at the root.

---

## Multi-provider detection

8 named providers + 1 sentinel:

| Provider | State | Solvable by SPECTER? | Reason |
|---|---|---|---|
| **cloudflare** | turnstile / managed / js | ✓ (delegates to `cf_selenium`) | the only one with a real solve path |
| **aws_waf** | js_challenge | ✗ (token lifecycle only) | token is provided out-of-band; we just manage it |
| **akamai** | human_required | ✗ | Bot Manager requires commercial solver |
| **datadome** | captcha | ✗ | requires commercial solver |
| **imperva** | js_challenge | ✗ | requires commercial solver |
| **recaptcha** | human_required | ✗ (escalates immediately) | privacy/ethics — never auto-solve |
| **hcaptcha** | human_required | ✗ (escalates immediately) | privacy/ethics — never auto-solve |
| **arkose** | human_required | ✗ (escalates immediately) | privacy/ethics — never auto-solve |
| **unknown** | (sentinel) | — | confidence < threshold; no handler called |

**The `HumanRequiredError` is a feature, not a bug.** When a reCAPTCHA / hCaptcha / Arkose challenge is detected, the agent stops gracefully with `aborted_reason: human_required`. Trace + screenshot is preserved. The user reviews the trace, opens the URL in a real browser, completes the challenge, and re-runs the agent — or feeds the resulting cookies in via `BypassSession`.

---

## AWS WAF token lifecycle

This is the only provider where SPECTER does *useful* work beyond detection. The model is:

1. You obtain a valid `aws-waf-token` out-of-band (e.g. a solved challenge from your staging environment, or a token from a partner integration).
2. You register it with the store: `adapter.store_token(value=..., host="staging.example.com", max_age=3600, source="manual")`.
3. You attach it to the session: `bs.apply_aws_waf_token("staging.example.com", value=..., expires_in=3600)`.
4. The agent visits the page. The AWS WAF adapter's `solve()` reads the token from the store, returns the cookies dict, and injects them via cf_selenium.
5. On 401/403/logout, you call `adapter.invalidate_token(host, reason="401")` and re-acquire.

```python
from specter.providers import AWSWAFAdapter, get_token_store
from specter.sessions import BypassSession

adapter = AWSWAFAdapter()
adapter.store_token("eyJhbGciOi...", host="staging.example.com", source="manual")

bs = BypassSession()
bs.apply_aws_waf_token("staging.example.com", "eyJhbGciOi...", expires_in=3600, source="manual")

# check state
state = adapter.token_state("staging.example.com")
print(state["has_usable_token"], state["needs_refresh"])

# invalidate
adapter.invalidate_token("staging.example.com", reason="401")
```

See `examples/aws_waf_token_lifecycle.py` for the full walkthrough.

---

## Vision decision layer

Vision is **decision-only**. It never clicks, types, or submits. It observes the page and reports:

```json
{
  "ran": true,
  "challenge_visible": true,
  "provider_hint": "aws_waf",
  "confidence": 0.7,
  "ocr_text": "AWS WAF Mock...",
  "sources": ["dom", "tesseract"],
  "latency_ms": 142
}
```

The planner consumes the verdict:
- `confidence < 0.6` → don't act on it
- `0.6 ≤ confidence < 0.8` → may escalate to `wait`
- `confidence ≥ 0.8` → treated as authoritative

Provider priority (auto-pick by task):
- `vision_complex` (multi-element, spatial reasoning) → **Claude Vision** (paid; needs `ANTHROPIC_API_KEY`)
- `vision_simple` (single text/button, OCR) → **Tesseract** (local, free; needs `tesseract` on PATH)
- Always available: **DOM** (free, reads HTML/classes/ids/names/src from `b.html`)

When neither vision is configured, the agent runs in **DOM-only mode** and still detects everything correctly via the signature cascade.

---

## Authorization model

SPECTER is opinionated about authorization. It will not run against arbitrary targets.

```python
# Blocked — empty allowed_domains
with AIWebAgent(allowed_domains=[]) as agent:
    agent.run("https://example.com", "...")  # → rejected at auth check

# Allowed — staging only
with AIWebAgent(allowed_domains=["staging.example.com"], authorized_test_mode=True) as agent:
    agent.run("https://staging.example.com/...", "...")  # → runs
```

Bypass is OFF by default (`bypass.enabled = false` in `~/.cf_agent/config.json`). Enable it explicitly for each domain. The bundled `mock_server.py` only listens on `127.0.0.1` and never serves a real challenge.

---

## Project layout

```
specter/
├── specter/                   # the package
│   ├── __init__.py
│   ├── __main__.py            # CLI entry
│   ├── agent.py               # AIWebAgent (main loop)
│   ├── config.py              # BypassConfig + env override
│   ├── sessions.py            # BypassSession (namespace provider:host)
│   ├── tools.py               # extract / plan / browse / session
│   ├── mock_server.py         # stdlib staging server (20 endpoints)
│   ├── providers/             # 8 named + 1 sentinel
│   │   ├── __init__.py
│   │   ├── base.py            # ProviderAdapter + ProviderId + ChallengeState + HumanRequiredError
│   │   ├── detector.py        # signature cascade + browser fallback
│   │   ├── registry.py
│   │   ├── cf_adapter.py
│   │   ├── aws_waf_adapter.py
│   │   ├── aws_waf_token.py   # token dataclass + persistent store
│   │   ├── akamai_adapter.py
│   │   ├── datadome_adapter.py
│   │   ├── imperva_adapter.py
│   │   ├── recaptcha_adapter.py
│   │   ├── hcaptcha_adapter.py
│   │   └── arkose_adapter.py
│   └── vision/                # Tesseract + Claude + DOM
│       └── __init__.py
├── cf_selenium.py             # FROZEN — browser control (do not modify)
├── cf_persistent.py           # FROZEN — session/cookie storage (do not modify)
├── tests/                     # standalone test scripts (TestContext runner)
│   ├── test_prod_style.py        # 9/9
│   ├── test_real_sites.py        # 6/6
│   ├── test_vision.py            # 3/3
│   ├── test_cf_agent_vision.py   # 3/3
│   ├── test_aws_waf_token.py     # 8/8
│   └── test_e2e_integration.py   # 4/4
├── examples/                  # runnable walkthroughs
│   ├── basic_visit.py
│   ├── aws_waf_token_lifecycle.py
│   └── vision_enabled.py
├── .github/
│   └── workflows/ci.yml
├── .gitignore
├── LICENSE
├── README.md
├── CHANGELOG.md
└── pyproject.toml
```

---

## Test results

| Suite | Tests | Result | Runtime (6GB Android) | Notes |
|---|---|---|---|---|
| `test_aws_waf_token` | 8 | **8/8 PASS** | ~3s | no browser; pure token/session |
| `test_vision` | 3 | **3/3 PASS** | ~50s | subprocess per subtest (memory isolation) |
| `test_cf_agent_vision` | 3 | **3/3 PASS** | ~50s | vision + agent integration |
| `test_prod_style` | 9 | **9/9 PASS** | ~4 min | full agent loop on mock server |
| `test_real_sites` | 6 | **6/6 PASS** | ~1 min | real public sites (no protection) |
| `test_e2e_integration` | 4 | **4/4 PASS** | ~65s | vision + multi-provider + AWS WAF + session |
| **Total** | **33** | **33/33 PASS** | ~6–7 min | |

---

## Roadmap

- [ ] Real solver for non-CF providers via pluggable adapter (commercial solver integration)
- [ ] JA3/JA4 TLS fingerprint detection
- [ ] IP reputation / residential proxy abstraction
- [ ] Cross-provider session sharing
- [ ] `pytest` migration of the test suite (currently standalone scripts)
- [ ] Optional WebSocket-based real-time trace streaming

---

## Ethics

This project is for **authorized security testing of systems you own or have explicit written permission to test**. Detection ≠ exploitation. The challenges are *classified* but never *solved* automatically. Tokens are *managed* but never *generated*. Cookies are *read* but never *forged*.

If you use this against an unauthorized target, you are on your own. The LICENSE includes an explicit ethics clause binding on top of MIT.

---

## License

MIT + ethics clause — see [LICENSE](LICENSE).

---

## Pushing to GitHub

If you cloned an existing repo, you can skip this. For first-time setup:

```bash
# 1. create a new empty repo on github.com (no README/.gitignore/license)
#    e.g. https://github.com/new → name: specter, owner: letticha

# 2. add the remote
cd specter
git remote add origin [email protected]:letticha/specter.git

# 3. push
git push -u origin main
```

If GitHub rejects the push (no auth), set up an SSH key or personal access token:

```bash
# option A: SSH key
ssh-keygen -t ed25519 -C "[email protected]"
# add ~/.ssh/id_ed25519.pub to github.com/settings/keys

# option B: PAT (HTTPS)
git remote set-url origin https://github.com/letticha/specter.git
# create a token at github.com/settings/tokens (classic, scope: repo)
# then either:
#   git push https://<token>@github.com/letticha/specter.git main
# or store in ~/.git-credentials after `git config --global credential.helper store`
```

To enable CI (`.github/workflows/ci.yml`): Settings → Actions → Allow all actions. CI runs on every push: syntax check, ruff lint, no-browser tests (token + detector).
