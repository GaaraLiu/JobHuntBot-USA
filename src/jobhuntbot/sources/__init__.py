"""Public ATS adapters for Phase 2 job discovery."""

from .base import (
    DiscoveryTarget,
    JsonHttpClient,
    PoliteJsonClient,
    SourceAdapter,
    SourceBatch,
    SourceConfigurationError,
    SourceFailure,
    SourceJob,
    SourceRequestError,
    SourceSalary,
)
from .registry import create_adapter, supported_sources
from .workday import WorkdayAdapter

__all__ = [
    "DiscoveryTarget",
    "JsonHttpClient",
    "PoliteJsonClient",
    "SourceAdapter",
    "SourceBatch",
    "SourceConfigurationError",
    "SourceFailure",
    "SourceJob",
    "SourceRequestError",
    "SourceSalary",
    "WorkdayAdapter",
    "create_adapter",
    "supported_sources",
]
