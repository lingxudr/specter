# SPECTER — AI Web-Agent for Cloudflare & Anti-Bot Protection

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Termux-Android-green?logo=android&logoColor=white" alt="Termux Android">
  <img src="https://img.shields.io/badge/Cloudflare-Bypassed-brightgreen" alt="Cloudflare Bypassed">
  <img src="https://img.shields.io/badge/hCaptcha-Detected-orange" alt="hCaptcha Detected">
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License">
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/4a3c8e2e-5f1a-43a2-9c3e-1234567890ab" alt="SPECTER Demo" width="600">
</p>

**SPECTER** is a Python-based AI web-agent that:
- **Bypasses Cloudflare** (Turnstile, JS Challenge, Managed Challenge)
- **Detects anti-bot protections** (hCaptcha, reCAPTCHA, AWS WAF, Akamai, DataDome, Imperva, Arkose)
- **Manages AWS WAF token lifecycle** (store, load, invalidate, refresh)
- **Provides vision decision layer** (DOM + Tesseract OCR + optional Claude Vision)
- **Captures full trace + screenshots** for post-mortem review

> **⚠️ SPECTER is for authorized staging/testing only.** Never use on production sites without permission.

---

## 🚀 Features

| Feature | Status | Description |
|--------|--------|-------------|
| **Cloudflare Auto-Solve** | ✅ | Solves Turnstile, JS Challenge, Managed Challenge |
| **hCaptcha/reCAPTCHA Detection** | ✅ | Detects v2/v3, invisible mode |
| **AWS WAF Token Lifecycle** | ✅ | Store, load, invalidate, refresh tokens |
| **Vision Decision Layer** | ✅ | DOM + Tesseract OCR + Claude Vision (optional) |
| **Multi-Provider Detection** | ✅ | 8 named providers + UNKNOWN sentinel |
| **Mobile Fingerprint** | ✅ | Termux-friendly mobile Android fingerprint |
| **Trace & Screenshots** | ✅ | Full per-step trace + screenshots |

---

## 📦 Installation (Termux/Android 13)

```bash
pkg update -y && pkg upgrade -y
pkg install -y python chromium tesseract ffmpeg
pip install --upgrade pip
pip install -e .  # from ~/specter
```

---

## 🛠️ Quick Start

### 1. Start Mock Server (for testing)
```bash
cd ~/specter
python -m specter --serve-mock --port 18801
# Verify: curl http://127.0.0.1:18801/healthz
```

### 2. Run Agent (Python API)
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

### 3. Run Agent (CLI)
```bash
python -m specter "https://staging.example.com" "what is the title?" \
    --allow staging.example.com --no-llm --max-steps 2
```

---

## 📂 Project Structure

```
specter/
├── specter/                  # Main Python package
│   ├── __init__.py
│   ├── agent.py              # AIWebAgent class
│   ├── config.py             # BypassConfig + env overrides
│   ├── sessions.py           # BypassSession (provider:host namespace)
│   ├── tools.py              # Tool layer (extract/plan/browse/session)
│   ├── mock_server.py        # 20 mock endpoints (127.0.0.1 only)
│   ├── providers/            # 8 named adapters + sentinel
│   │   ├── cf_adapter.py
│   │   ├── hcaptcha_adapter.py
│   │   ├── recaptcha_adapter.py
│   │   ├── aws_waf_adapter.py
│   │   └── ...
│   └── vision/               # Vision decision layer
│       ├── tesseract.py
│       ├── claude_vision.py
│       └── dom.py
├── cf_selenium.py            # FROZEN — Selenium-style API + CF bypass
├── cf_persistent.py          # FROZEN — SQLite session/cookie store
├── tests/                    # Standalone test scripts
├── examples/                 # Runnable walkthroughs
└── README.md                 # Project docs
```

---

## 🔍 Detection Providers

| Provider | Auto-Solvable | State | Notes |
|----------|---------------|-------|-------|
| **Cloudflare** | ✅ | turnstile/managed/js | Auto-solved by `cf_selenium` |
| **hCaptcha** | ❌ | human_required | Detection only (no auto-solve) |
| **reCAPTCHA** | ❌ | human_required | Detection only (no auto-solve) |
| **AWS WAF** | ✅ | js_challenge | Token lifecycle only |
| **Akamai** | ❌ | human_required | Bot Manager |
| **DataDome** | ❌ | human_required | CAPTCHA |
| **Imperva** | ❌ | human_required | JS challenge |
| **Arkose** | ❌ | human_required | Human interaction required |
| **UNKNOWN** | ❌ | human_required | Confidence < threshold |

---

## 📸 Output Artifacts

Per-run artifacts are saved to:
```
~/.cf_agent/runs/<run_id>/
├── trace.jsonl          # One event per action
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

## 📚 Documentation

| File | Description |
|------|-------------|
| [references/vision.md](references/vision.md) | Vision API + 6GB OOM pattern |
| [references/testing-pitfalls.md](references/testing-pitfalls.md) | 15 bugs that bit during dev |
| [scripts/recon_captcha_api.py](scripts/recon_captcha_api.py) | Discover captcha sitekey & API enforcement |

---

## 🧪 Testing

### Run Mock Server
```bash
python -m specter --serve-mock --port 18801
curl http://127.0.0.1:18801/healthz  # should return 200
```

### Run Test Suite
```bash
cd ~/specter
python test_prod_style.py  # 9/9 PASS expected
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

- **GitHub**: [github.com/lingxudr/specter](https://github.com/lingxudr/specter)
- **Discord**: [Discord Server](https://discord.gg/m9ZRBTZvbr)

---

<p align="center">
  <img src="https://github.com/user-attachments/assets/12345678-1234-1234-1234-1234567890ab" alt="SPECTER Demo 2" width="600">
</p>

**🚀 Ready for authorized staging testing. Never use on production without permission.**