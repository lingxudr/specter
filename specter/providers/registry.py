"""registry.py — Provider registry singleton."""
from __future__ import annotations

from typing import Iterator

from .base import ProtectionProvider, ProviderId


class ProviderRegistry:
    """Registry of all known ProtectionProvider adapters.

    Keyed by provider_id. The UNKNOWN provider is NEVER registered.
    """

    def __init__(self):
        self._providers: dict[str, ProtectionProvider] = {}

    def register(self, provider: ProtectionProvider) -> None:
        if not isinstance(provider, ProtectionProvider):
            raise TypeError(f"expected ProtectionProvider, got {type(provider).__name__}")
        if not provider.provider_id:
            raise ValueError("provider.provider_id must be set")
        if provider.provider_id == ProviderId.UNKNOWN.value:
            raise ValueError("cannot register UNKNOWN provider")
        if provider.provider_id in self._providers:
            # re-registration is allowed (overwrites); useful for tests
            pass
        self._providers[provider.provider_id] = provider

    def unregister(self, provider_id: str) -> bool:
        return self._providers.pop(provider_id, None) is not None

    def get(self, provider_id: str) -> ProtectionProvider | None:
        return self._providers.get(provider_id)

    def has(self, provider_id: str) -> bool:
        return provider_id in self._providers

    def all_providers(self) -> list[ProtectionProvider]:
        return list(self._providers.values())

    def all_ids(self) -> list[str]:
        return list(self._providers.keys())

    def __len__(self) -> int:
        return len(self._providers)

    def __iter__(self) -> Iterator[ProtectionProvider]:
        return iter(self._providers.values())

    def __contains__(self, provider_id: str) -> bool:
        return provider_id in self._providers


_singleton: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    """Get the global registry singleton."""
    global _singleton
    if _singleton is None:
        _singleton = ProviderRegistry()
    return _singleton


def reset_registry() -> None:
    """Reset the singleton (useful for tests)."""
    global _singleton
    _singleton = None
