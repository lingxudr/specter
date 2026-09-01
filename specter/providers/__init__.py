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

# Pre-register all 8 named adapters. UNKNOWN is the sentinel and never registered.
get_registry().register(CloudflareAdapter())
get_registry().register(AkamaiAdapter())
get_registry().register(DataDomeAdapter())
get_registry().register(ImpervaAdapter())
get_registry().register(AWSWAFAdapter())
get_registry().register(ReCaptchaAdapter())
get_registry().register(HCaptchaAdapter())
get_registry().register(ArkoseAdapter())

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
]
