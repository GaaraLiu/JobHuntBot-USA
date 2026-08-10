"""Configuration loading and validation for the matching core."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """Raised when configuration is missing or internally inconsistent."""


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


@dataclass(frozen=True, slots=True)
class Thresholds:
    apply_min: float
    review_min: float
    minimum_apply_coverage: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    schema_version: int
    thresholds: Thresholds
    scoring_weights: dict[str, float]
    normalization: dict[str, Any]
    parser: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AppConfig":
        try:
            threshold_data = value["thresholds"]
            thresholds = Thresholds(
                apply_min=float(threshold_data["apply_min"]),
                review_min=float(threshold_data["review_min"]),
                minimum_apply_coverage=float(threshold_data["minimum_apply_coverage"]),
            )
            weights = {str(k): float(v) for k, v in value["scoring_weights"].items()}
            config = cls(
                schema_version=int(value.get("schema_version", 1)),
                thresholds=thresholds,
                scoring_weights=weights,
                normalization=dict(value.get("normalization", {})),
                parser=dict(value.get("parser", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid JobHuntBot configuration: {exc}") from exc
        config.validate()
        return config

    def validate(self) -> None:
        if self.schema_version < 1:
            raise ConfigError("schema_version must be a positive integer")
        if not 0 <= self.thresholds.review_min <= self.thresholds.apply_min <= 100:
            raise ConfigError("thresholds must satisfy 0 <= review_min <= apply_min <= 100")
        if not 0 <= self.thresholds.minimum_apply_coverage <= 1:
            raise ConfigError("minimum_apply_coverage must be between 0 and 1")
        if not self.scoring_weights or any(weight < 0 for weight in self.scoring_weights.values()):
            raise ConfigError("scoring_weights must be present and non-negative")
        total = sum(self.scoring_weights.values())
        if abs(total - 100.0) > 1e-9:
            raise ConfigError(f"scoring_weights must sum to 100, got {total:g}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not load configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Configuration root must be an object: {path}")
    return value


def load_config(override_path: str | Path | None = None) -> AppConfig:
    """Load packaged defaults, optionally deep-merging a user JSON override."""

    default_resource = resources.files("jobhuntbot").joinpath("resources/default_config.json")
    with default_resource.open("r", encoding="utf-8") as handle:
        base = json.load(handle)
    if override_path is not None:
        base = _deep_merge(base, _load_json(Path(override_path)))
    return AppConfig.from_mapping(base)

