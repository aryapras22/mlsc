"""Discovers a ``SourceAdapter`` by name without other modules changing.

An adapter registers itself with ``@register("name")``; a caller looks it up
with ``get("name")``. Adding a new source touches only its own module plus one
registration line, never the registry's internals.
"""

from __future__ import annotations

from mlsc.sources.base import SourceAdapter

_REGISTRY: dict[str, type[SourceAdapter]] = {}


class UnknownSourceError(KeyError):
    """Raised when no adapter is registered under the requested name."""


def register(name: str):
    def decorator(adapter_class: type[SourceAdapter]) -> type[SourceAdapter]:
        _REGISTRY[name] = adapter_class
        return adapter_class

    return decorator


def get(name: str) -> type[SourceAdapter]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownSourceError(name) from None


def names() -> tuple[str, ...]:
    return tuple(_REGISTRY)
