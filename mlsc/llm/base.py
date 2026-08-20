"""Provider interface and Completion type, shaped against all three callers:
intent (document-enrichment), topic labels (persistent-topics), and
opportunities (insight-generation).

Completion wraps the value with its provenance so provenance cannot be
forgotten at the call site (design.md, "Domain shapes").
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class SchemaViolation(RuntimeError):
    """Output did not validate after the single retry."""


class ProviderUnreachable(RuntimeError):
    """Transport failed against the configured endpoint."""


@dataclasses.dataclass(frozen=True)
class Completion:
    """An LLM result plus the provenance every persisted LLM-derived value needs."""

    value: Any
    provider: str
    model: str
    prompt_version: str


class Provider(Protocol):
    """One interface regardless of vendor.

    ``complete`` covers all three tiers: intent needs batched classification
    against a schema, labelling a short constrained string, insight a long
    structured object — all expressible as "a prompt, a schema, a completion".
    """

    async def complete(
        self, *, prompt: str, schema: type[T], prompt_version: str
    ) -> Completion: ...
