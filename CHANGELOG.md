# Changelog

All notable changes to SPECTER are documented here. Versioning follows
[SemVer](https://semver.org/). The project is pre-1.0; minor version
bumps may include breaking changes.

## [0.5.0] — 2026-09-01

### Added
- **End-to-end integration test** (`tests/test_e2e_integration.py`, 4/4 PASS):
  vision + multi-provider detection + AWS WAF token lifecycle + session
  persistence in one run. 65s on 6GB Android, 4 sub-tests in fresh
  subprocesses for memory isolation.
- **AWS WAF token API** (`specter/providers/aws_waf_token.py`):
  `AWSWAFToken` dataclass + `AWSWAFTokenStore` persistent JSON at
  `~/.cf_agent/aws_waf_tokens.json`. Supports `store`, `load`,
  `invalidate`, `refresh`, `purge_expired`. Adapter exposes
  `token_state()`, `load_token()`, `store_token()`, `invalidate_token()`,
  `refresh_token()`. `solve()` never auto-obtains; raises
  `HumanRequiredError` if no usable token.
- **BypassSession AWS WAF integration** (`specter/sessions.py`): 5 new
  methods — `apply_aws_waf_token`, `has_aws_waf_token`,
  `get_aws_waf_token`, `invalidate_aws_waf_token`, `refresh_aws_waf_token`.
  Token stored as cookie in the FROZEN `cf_persistent.SessionDB` + metadata
  in the existing `extras.json` (no schema change).
- **Mock server AWS WAF endpoints** (`specter/mock_server.py`):
  `GET /aws-waf/refresh` (rotates token), `GET /aws-waf/invalidate`
  (server-side reject), `GET /aws-waf/expired` (1s Max-Age).
- **Vision DOM provider** (`specter/vision/__init__.py`): regex now
  scans `class=`, `name=`, `id=`, `src=`, `data-sitekey=`. Real AWS WAF
  pages use `name="aws-waf-token"` / `id="aws-waf-challenge"`, not
  class attributes. Also added `awswaf` / `aws-waf` to Tesseract
  keyword map.
- **Agent snapshot HTML enrichment** (`specter/agent.py`): all
  `b.snapshot()` calls → `b.snapshot(include_html=True)`. Without
  HTML, signature detection + DOM vision see zero raw HTML markers
  from cf_selenium's CDP snapshot.
- **CLI entry point** (`specter/__main__.py`): `python -m specter ...`
  dispatches to the agent, task JSON, or mock server. Argument
  validation + help included.

### Fixed
- DOM vision provider was returning empty label for AWS WAF pages
  because the cf_selenium snapshot does not include raw HTML by
  default and the regex only matched `class=`. Now matches five
  attribute names.
- Agent's `current_provider` is on the agent object, not in the
  `AgentResult` dataclass, so `asdict(ar)` did not include it. Workers
  now explicitly set it.

### Tests
- 33/33 PASS across 6 test files (no regression).
- 4 key fixes documented in the skill so the next iteration does not
  re-discover them.

## [0.4.0] — 2026-08-31

### Added
- **Vision layer** (`specter/vision/`): Tesseract (local OCR) +
  Claude Vision (paid; `ANTHROPIC_API_KEY` gated) + DOM (free, HTML
  attribute regex). Auto-pick by `VisionTask` (complex → Claude,
  simple → Tesseract, always-available → DOM).
- **Vision → cf_agent integration**: `use_vision` param on
  `AIWebAgent`, `vision_consult()` per-step helper, verdict attached
  to `snap["vision_verdict"]` for the planner, confidence policy
  (`< 0.6` ignore, `0.6–0.8` retry, `≥ 0.8` authoritative), trace
  event `vision` field, `vision_log` in `result.json`.
- **Multi-provider detection cascade** (`specter/providers/detector.py`):
  signature → browser snapshot fallback. Cache keyed by host with
  5-minute TTL. Disambiguated hCaptcha vs reCAPTCHA markers (the
  `data-sitekey` attribute is shared).
- **Provider abstraction**: 8 named adapters + 1 sentinel
  (`unknown`). Common interface: `detect()`, `solve()`, `state()`,
  `auto_solvable` flag, `requires_human` flag.
- **BypassSession** (`specter/sessions.py`): namespace
  `provider:host`. 3 records can coexist for the same host with
  different providers.
- **Tool layer** (`specter/tools.py`): `extract` / `plan` / `browse`
  / `session` with bypass-flag gating (read-only tools don't
  require bypass).
- **Mock staging server** (`specter/mock_server.py`): 17 endpoints
  covering all 8 providers + 1 unprotected page + healthz.
- **Production-style test suite** (`tests/test_prod_style.py`, 9/9
  PASS): snapshot, form fill, multi-step nav, screenshot, recording,
  challenge flow, agent loop, agent dashboard, multi-provider
  detection.
- **Real-world public site test suite** (`tests/test_real_sites.py`,
  6/6 PASS): example.com, example.org, iana.org, httpbin.org,
  info.cern.ch (subpage navigation).
- **Vision test suite** (`tests/test_vision.py`, 3/3 PASS): runs
  each sub-test in a fresh subprocess to free Chrome memory on 6GB.

### Fixed
- Tesseract-on-Termux: install via `apt install tesseract` directly
  (not via proot — proot Ubuntu + Chrome = OOM on 6GB).
- DOM provider's `b.cookies` included iframe 3p cookies; filtered by
  document host.
- Provider enum hygiene: `provider_id = ProviderId.X.value` returns
  a string for serialization, but the constructor expects the enum;
  use the bare enum in assignments and `.value` only for dict output.

## [0.1.0] — 2026-08-15

### Added
- Initial project skeleton: `AIWebAgent` (single-CF scope), Selenium-
  style `cf_selenium.Browser`, `cf_persistent.SessionDB`, snapshot
  loop with trace + screenshots + run_id artifacts.
- justpaste.it smoke test, nowsecure.nl CF Turnstile smoke (auto-
  solve via cf_selenium).
- LLM integration via routerku on `http://127.0.0.1:20130` (model
  `Free-All`).
- Config loader with env-var priority: `CF_BYPASS_ENABLED`,
  `CF_BYPASS_ALLOW`, `CF_AGENT_MAX_STEPS`, `CF_AGENT_MODEL`.
