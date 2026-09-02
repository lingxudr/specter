# SPECTER — AI Web-Agent for Cloudflare Bypass & Protection Detection

**SPECTER** is a Python-based AI web-agent built on top of `cf_selenium` (frozen) that:
- Detects anti-bot protection providers (Cloudflare, reCAPTCHA, hCaptcha, AWS WAF, Imperva, Akamai, DataDome, Arkose)
- Auto-solves **Cloudflare challenges** (Turnstile, JS challenge, Managed Challenge)
- Manages **AWS WAF token lifecycle** (store, load, invalidate, refresh)
- Provides a **vision decision layer** (DOM + Tesseract OCR + optional Claude Vision) for challenge classification
- Captures full trace + screenshots per step for post-mortem review

**SPECTER is NOT a bypass tool** for hCaptcha/reCAPTCHA/Arkose/Akamai/DataDome/Imperva. For those, you need commercial solvers (2Captcha, AntiCaptcha, CapSolver) or human interaction. SPECTER's role is **detection + token lifecycle + audit**.

---

## 🚀 Quick Start (Termux/Android 13)

### 1. Install Dependencies
```bash
pkg update -y && pkg upgrade -y
pkg install -y python chromium tesseract ffmpeg
pip install --upgrade pip
pip install -e .  # from ~/specter
```

### 2. Start Mock Server (for testing)
```bash
cd ~/specter
python -m specter --serve-mock --port 18801
# Verify: curl http://127.0.0.1:18801/healthz
```

### 3. Run Agent (Python API)
```python
from specter import AIWebAgent

with AIWebAgent(
    profile="agent",
    allowed_domains=["staging.example.com", "127.0.0.1"],
    authorized_test_mode=True,
    use_llm=True,
) as agent:
    result = agent.run(
        "https://staging.example.com",
        "click the first product and add to cart"
    )
    print(result.summary, result.final_url, result.artifacts_dir)
```

### 4. Run Agent (CLI)
```bash
python -m specter "https://staging.example.com" "what is the title?" \
    --allow staging.example.com --no-llm --max-steps 2

python -m specter "https://staging.example.com" "click checkout, fill form" \
    --allow staging.example.com --max-steps 8
```

---

## 📂 Project Structure

```
specter/
├── specter/                  # Main Python package
│   ├── __init__.py
│   ├── __main__.py           # CLI entry
│   ├── agent.py              # AIWebAgent class
│   ├── config.py             # BypassConfig + env overrides
│   ├── sessions.py           # BypassSession (provider:host namespace)
│   ├── tools.py              # Tool layer (extract/plan/browse/session)
│   ├── mock_server.py        # 20 mock endpoints (127.0.0.1 only)
│   ├── providers/            # 8 named adapters + sentinel
│   │   ├── __init__.py
│   │   ├── base.py           # ProviderAdapter + ProviderId + ChallengeState
│   │   ├── detector.py       # signature cascade + browser fallback
│   │   ├── registry.py
│   │   ├── cf_adapter.py
│   │   ├── aws_waf_adapter.py
│   │   ├── aws_waf_token.py  # token dataclass + persistent store
│   │   ├── akamai_adapter.py
│   │   ├── datadome_adapter.py
│   │   ├── imperva_adapter.py
│   │   ├── recaptcha_adapter.py
│   │   ├── hcaptcha_adapter.py
│   │   └── arkose_adapter.py
│   └── vision/               # Vision decision layer
│       ├── __init__.py
│       ├── claude_vision.py
│       ├── tesseract.py
│       └── dom.py
├── cf_selenium.py            # FROZEN — Selenium-style API + CF bypass
├── cf_persistent.py          # FROZEN — SQLite session/cookie store
├── tests/                    # Standalone test scripts
├── examples/                 # Runnable walkthroughs
└── README.md                 # This file
```

---

## 🛠️ Key Features

| Feature | Status | Description |
|---|---|---|
| **Cloudflare Auto-Solve** | ✅ | Solves Turnstile, JS Challenge, Managed Challenge |
| **reCAPTCHA Detection** | ✅ | Detects v2/v3, invisible mode |
| **hCaptcha Detection** | ✅ | Detects v2, invisible mode |
| **AWS WAF Token Lifecycle** | ✅ | Store, load, invalidate, refresh tokens |
| **Vision Decision Layer** | ✅ | DOM + Tesseract OCR + Claude Vision (optional) |
| **Multi-Provider Detection** | ✅ | 8 named providers + UNKNOWN sentinel |
| **Trace & Screenshots** | ✅ | Full per-step trace + screenshots |
| **Mobile Fingerprint** | ✅ | Termux-friendly mobile Android fingerprint |

---

## 🔍 Detection Providers

| Provider | Auto-Solvable | State | Notes |
|---|---|---|---|
| **cloudflare** | ✅ | turnstile/managed/js | Auto-solved by `cf_selenium` |
| **akamai** | ❌ | human_required | Bot Manager |
| **datadome** | ❌ | human_required | CAPTCHA |
| **imperva** | ❌ | human_required | JS challenge |
| **aws_waf** | ✅ | js_challenge | Token lifecycle only |
| **recaptcha** | ❌ | human_required | Invisible mode needs solver |
| **hcaptcha** | ❌ | human_required | Invisible mode needs solver |
| **arkose** | ❌ | human_required | Human interaction required |
| **unknown** | ❌ | human_required | Confidence < threshold |

---

## 📁 Outputs

Per-run artifacts are saved to:
```
~/.cf_agent/runs/<run_id>/
├── trace.jsonl          # One event per action (action, target, URL, timestamp, result, extra)
├── result.json          # Final summary
└── screenshots/         # step{N}_before.png, step{N}_after.png
```

Example `result.json`:
```json
{
  "summary": "Clicked product and added to cart",
  "final_url": "https://staging.example.com/cart",
  "artifacts_dir": "/data/data/com.termux/files/home/.cf_agent/runs/run_123456/",
  "trace_events": 8,
  "screenshots": 8,
  "current_provider": "cloudflare",
  "aborted_reason": null
}
```

---

## 🚨 Important Notes

### 1. **SPECTER is for authorized staging only**
- **Never** use on production sites without explicit permission.
- **Never** auto-register accounts or bypass authentication.
- **Never** fabricate tokens or credentials.

### 2. **Fingerprint Stealth**
- Mobile Android fingerprint is applied by default (Termux-friendly).
- `navigator.webdriver` is masked.
- Screen dimensions, touch events, and connection type match real devices.

### 3. **hCaptcha/reCAPTCHA**
- **Detection works**, but **auto-solve is not provided** (ethics + complexity).
- If detected, agent raises `HumanRequiredError` with a hint for manual solving.
- For real bypass, use commercial solvers (2Captcha, AntiCaptcha, CapSolver).

### 4. **AWS WAF Token**
- **Token must be obtained out-of-band** (legitimately).
- SPECTER only manages the lifecycle (store, load, invalidate, refresh).
- Never auto-obtain tokens.

### 5. **Memory Management (Termux 6GB)**
- Each Chrome instance + Tesseract + Pillow eats ~500MB.
- Run each subtest in a **fresh subprocess** to free memory.
- Limit Chrome instances to 1-2 max.

---

## 📚 Documentation

| File | Description |
|---|---|
| [references/vision.md](references/vision.md) | Full `cf_agent_vision` API + 6GB OOM pattern |
| [references/testing-pitfalls.md](references/testing-pitfalls.md) | 15 concrete bugs that bit during dev |
| [references/specter-pitfalls.md](references/specter-pitfalls.md) | Operational pitfalls + xkiro.com validation |
| [scripts/recon_captcha_api.py](scripts/recon_captcha_api.py) | Discover backend host, captcha sitekey, API-level enforcement |

---

## 🧪 Testing

### Run Mock Server
```bash
python -m specter --serve-mock --port 18801
curl http://127.0.0.1:18801/healthz  # should return 200
```

### Run Tests
```bash
cd ~/specter
# Start mock server in background
python -m specter --serve-mock --port 18801 &

# Run test suite
python test_prod_style.py  # 9/9 PASS expected
```

### Test Artifacts
```
~/test_logs/run_<timestamp>_<rand>/
├── summary.json
├── 01_snapshot/
├── 02_fill_form/
└── ...
```

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🤝 Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/xxx`)
3. Commit your changes (`git commit -m 'feat: add xxx'`)
4. Push to the branch (`git push origin feat/xxx`)
5. Open a Pull Request

---

## 📬 Contact

For issues, questions, or contributions:
- GitHub: [github.com/letticha/specter](https://github.com/letticha/specter)
- Discord: [Discord link](https://discord.gg/m9ZRBTZvbr)

---

## 📝 Changelog

### v6 (Latest)
- ✅ **hCaptcha/reCAPTCHA detection** + helper flow (render widget, wait for user)
- ✅ **Mobile Android fingerprint** (Termux-friendly)
- ✅ **Vision decision layer** (DOM + Tesseract + Claude Vision)
- ✅ **AWS WAF token lifecycle** (store, load, invalidate, refresh)
- ✅ **Multi-provider detection** (8 named + UNKNOWN)
- ✅ **Trace & screenshots** per step
- ✅ **Memory management** for Termux 6GB

### v5
- ✅ **End-to-end integration** (vision + multi-provider + AWS WAF token + session)
- ✅ **Test suite** (9/9 PASS)

### v4
- ✅ **Vision layer** (DOM + Tesseract OCR + Claude Vision)

### v3
- ✅ **Production-style tests** (real `cf_selenium` + Chrome 130)

### v2
- ✅ **Multi-provider detection** (8 adapters)

### v1
- ✅ **Cloudflare auto-solve** + basic agent

---

## 📌 Quick Reference

| Command | Description |
|---|---|
| `python -m specter --serve-mock --port 18801` | Start mock server |
| `python -m specter "URL" "goal" --allow DOMAIN` | Run agent |
| `python test_prod_style.py` | Run test suite |
| `curl http://127.0.0.1:18801/healthz` | Verify mock server |

---

**🚀 Ready for authorized staging testing. Never use on production without permission.**