"""cf_agent_providers — Multi-provider protection abstraction.

Public API:
    from specter.providers import (
        ProviderId, ChallengeState, DetectionResult,
        ProtectionProvider, get_registry, get_detector,
    )

    detector = get_detector()
    result = detector.detect_signature(headers=..., body=..., host=...)
    if result.confidence >= 0.6 and result.provider != ProviderId.UNKNOWN:
        adapter = get_registry().get(result.provider)
        ...
"""
from .base import (
    ProviderId,
    ChallengeState,
    DetectionResult,
    SessionInfo,
    ProtectionProvider,
    HumanRequiredError,
    ProviderNotSolvableError,
)
from .registry import ProviderRegistry, get_registry
from .detector import ProviderDetector, get_detector, DetectionCache
from .cf_adapter import CloudflareAdapter
from .akamai_adapter import AkamaiAdapter
from .datadome_adapter import DataDomeAdapter
from .imperva_adapter import ImpervaAdapter
from .aws_waf_token import AWSWAFToken, AWSWAFTokenStore, get_token_store
from .aws_waf_adapter import AWSWAFAdapter
from .recaptcha_adapter import ReCaptchaAdapter
from .hcaptcha_adapter import HCaptchaAdapter
from .arkose_adapter import ArkoseAdapter
from .hcaptcha_solver_adapter import HCaptchaSolverAdapter
from .recaptcha_solver_adapter import ReCaptchaSolverAdapter

# Pre-register all 8 named adapters. UNKNOWN is the sentinel and never registered.
# hCaptcha/reCAPTCHA: prefer the SOLVER adapter (auto_solvable=True) over the
# human-required one. The solver still inherits all detection logic from the
# base adapter, so detection confidence is unchanged.
get_registry().register(CloudflareAdapter())
get_registry().register(AkamaiAdapter())
get_registry().register(DataDomeAdapter())
get_registry().register(ImpervaAdapter())
get_registry().register(AWSWAFAdapter())
get_registry().register(ReCaptchaAdapter())  # human-required fallback
get_registry().register(HCaptchaAdapter())    # human-required fallback
get_registry().register(ArkoseAdapter())

# Auto-solver variants — override the human-required behavior.
# Register LAST so they win the duplicate-key check (registry allows re-registration).
import os as _os
if _os.environ.get("SPECTER_CAPTCHA_SOLVER", "1") != "0":
    get_registry().register(HCaptchaSolverAdapter())
    get_registry().register(ReCaptchaSolverAdapter())

__all__ = [
    "ProviderId",
    "ChallengeState",
    "DetectionResult",
    "SessionInfo",
    "ProtectionProvider",
    "HumanRequiredError",
    "ProviderNotSolvableError",
    "ProviderRegistry",
    "get_registry",
    "ProviderDetector",
    "get_detector",
    "DetectionCache",
    "CloudflareAdapter",
    "AkamaiAdapter",
    "DataDomeAdapter",
    "ImpervaAdapter",
    "AWSWAFAdapter",
    "AWSWAFToken",
    "AWSWAFTokenStore",
    "get_token_store",
    "ReCaptchaAdapter",
    "HCaptchaAdapter",
    "ArkoseAdapter",
    "HCaptchaSolverAdapter",
    "ReCaptchaSolverAdapter",
]
