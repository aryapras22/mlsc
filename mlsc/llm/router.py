"""Maps a Tier to a configured Provider instance. Built once at startup."""

from __future__ import annotations

from enum import Enum

from openai import AsyncOpenAI

from mlsc.config import load_llm_tier_settings
from mlsc.llm.base import Provider
from mlsc.llm.providers.openai_compatible import OpenAiCompatibleProvider


class Tier(str, Enum):
    """Named by purpose, not by number — a tier called intent cannot be
    routed to the wrong prompt by an off-by-one (design.md, "Domain shapes")."""

    INTENT = "intent"
    LABELING = "labeling"
    INSIGHT = "insight"


class LlmRouter:
    def __init__(self, providers: dict[Tier, Provider]) -> None:
        self._providers = providers

    def for_tier(self, tier: Tier) -> Provider:
        return self._providers[tier]

    @classmethod
    def from_configuration(cls) -> LlmRouter:
        """Fails at startup if a tier is unconfigured, per TierNotConfigured's
        failure strategy — not deferred to first use."""
        providers: dict[Tier, Provider] = {}
        for tier in Tier:
            settings = load_llm_tier_settings(tier.value)
            client = AsyncOpenAI(
                base_url=settings.base_url, api_key=settings.api_key.get_secret_value()
            )
            providers[tier] = OpenAiCompatibleProvider(client, model=settings.model)
        return cls(providers)
