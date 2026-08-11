"""Registry for supported public ATS source adapters."""

from __future__ import annotations

from typing import Callable

from .ashby import AshbyAdapter
from .base import JsonHttpClient, SourceAdapter, SourceConfigurationError
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter
from .smartrecruiters import SmartRecruitersAdapter


AdapterFactory = Callable[[JsonHttpClient], SourceAdapter]

_ADAPTERS: dict[str, AdapterFactory] = {
    "smartrecruiters": SmartRecruitersAdapter,
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}


def supported_sources() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def create_adapter(source: str, client: JsonHttpClient) -> SourceAdapter:
    key = str(source or "").strip().casefold()
    try:
        factory = _ADAPTERS[key]
    except KeyError as exc:
        raise SourceConfigurationError(
            f"Unsupported source '{source}'. Supported sources: {', '.join(supported_sources())}."
        ) from exc
    return factory(client)
