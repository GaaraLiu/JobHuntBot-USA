"""Deterministic, explainable pre-scoring career-track relevance."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .models import NormalizedJob
from .normalization import normalize_text_key
from .sources import SourceJob


_DEFAULT_GENERIC_TERMS = [
    "data",
    "analysis",
    "technology",
    "business",
    "research",
    "engineering",
]

_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}

_COUNTRY_ALIASES = {
    "US": {"us", "usa", "united states", "united states of america"},
    "CA": {"ca", "can", "canada"},
    "GB": {"gb", "gbr", "uk", "united kingdom", "england", "scotland", "wales"},
    "AU": {"au", "aus", "australia"},
    "NZ": {"nz", "nzl", "new zealand"},
    "IE": {"ie", "irl", "ireland"},
    "DE": {"de", "deu", "germany"},
    "FR": {"fr", "fra", "france"},
    "NL": {"nl", "nld", "netherlands"},
    "IN": {"in", "ind", "india"},
    "SG": {"sg", "sgp", "singapore"},
    "JP": {"jp", "jpn", "japan"},
    "CN": {"cn", "chn", "china"},
    "BR": {"br", "bra", "brazil"},
    "MX": {"mx", "mex", "mexico"},
}

_LOCATION_COUNTRY_TERMS = {
    code: sorted(
        (term for term in aliases if len(term) > 2 or term in {"uk", "usa"}),
        key=len,
        reverse=True,
    )
    for code, aliases in _COUNTRY_ALIASES.items()
}


@dataclass(slots=True)
class CareerTrack:
    name: str
    enabled: bool = True
    aliases: list[str] = field(default_factory=list)
    title_terms: list[str] = field(default_factory=list)
    category_terms: list[str] = field(default_factory=list)
    description_terms: list[str] = field(default_factory=list)
    positive_terms: list[str] = field(default_factory=list)
    exclusion_terms: list[str] = field(default_factory=list)
    minimum_score: int | None = None
    minimum_description_matches: int = 2
    allow_seniority_levels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RelevancePolicy:
    enabled: bool = False
    title_weight: int = 4
    category_weight: int = 3
    description_weight: int = 1
    positive_weight: int = 1
    minimum_score: int = 4
    strong_score: int = 8
    generic_terms: list[str] = field(default_factory=lambda: list(_DEFAULT_GENERIC_TERMS))
    global_exclusion_terms: list[str] = field(default_factory=list)
    excluded_seniority_levels: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LocationPolicy:
    enabled: bool = False
    primary_markets: list[str] = field(default_factory=list)
    remote_terms: list[str] = field(default_factory=list)
    relocation_markets: list[str] = field(default_factory=list)
    exclusion_terms: list[str] = field(default_factory=list)
    allow_other_locations: bool = True
    allowed_countries: list[str] = field(default_factory=lambda: ["US"])


@dataclass(slots=True)
class RelevanceResult:
    matched_track: str = ""
    strength: str = "none"
    score: int = 0
    matched_terms: list[str] = field(default_factory=list)
    excluded: bool = True
    exclusion_type: str = "no_track_match"
    exclusion_terms: list[str] = field(default_factory=list)
    location_tier: str = "unknown"
    location_reason: str = ""
    reason: str = "No credible career-track evidence was found."

    @property
    def relevant(self) -> bool:
        return bool(self.matched_track and not self.excluded)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["relevant"] = self.relevant
        value["score_or_strength"] = self.strength
        return value


class JobRelevanceEvaluator:
    """Match meaningful configured evidence without generic-keyword inflation."""

    def __init__(
        self,
        tracks: Mapping[str, CareerTrack],
        policy: RelevancePolicy,
        location_policy: LocationPolicy | None = None,
    ):
        self.tracks = dict(tracks)
        self.policy = policy
        self.location_policy = location_policy or LocationPolicy()
        self.generic_terms = {
            normalize_text_key(term) for term in policy.generic_terms if normalize_text_key(term)
        }

    def evaluate(self, source_job: SourceJob, job: NormalizedJob) -> RelevanceResult:
        if not self.policy.enabled:
            return RelevanceResult(
                matched_track="legacy_unfiltered",
                strength="strong",
                score=0,
                matched_terms=[],
                excluded=False,
                exclusion_type="",
                location_tier="not_assessed",
                reason="Targeted relevance is disabled; Phase 2 MVP behavior is preserved.",
            )

        location_tier, location_exclusion, location_reason = self._location(source_job)
        title = normalize_text_key(source_job.title)
        global_exclusions = self._matches(self.policy.global_exclusion_terms, title)
        if global_exclusions:
            return RelevanceResult(
                excluded=True,
                exclusion_type="explicit_exclusion",
                exclusion_terms=global_exclusions,
                location_tier=location_tier,
                location_reason=location_reason,
                reason="Title matched configured global exclusion terms: "
                + ", ".join(global_exclusions),
            )
        if location_exclusion:
            return RelevanceResult(
                excluded=True,
                exclusion_type=(
                    "international" if location_tier == "international" else "location"
                ),
                location_tier=location_tier,
                location_reason=location_reason,
                reason=location_reason,
            )

        category = normalize_text_key(_metadata_text(source_job.metadata))
        description = normalize_text_key(source_job.description)
        candidates: list[tuple[int, int, str, list[str], CareerTrack]] = []
        configured_tracks = source_job.metadata.get("_discovery_tracks", [])
        target_tracks = {
            str(item) for item in configured_tracks if str(item).strip()
        } if isinstance(configured_tracks, list) else set()
        for order, (name, track) in enumerate(self.tracks.items()):
            if not track.enabled or (target_tracks and name not in target_tracks):
                continue
            title_terms = list(dict.fromkeys(track.aliases + track.title_terms))
            title_matches = self._meaningful_matches(title_terms, title)
            category_matches = self._meaningful_matches(track.category_terms, category)
            description_matches = self._meaningful_matches(
                track.description_terms, description
            )
            positive_matches = self._meaningful_matches(track.positive_terms, description)
            score = (
                len(title_matches) * self.policy.title_weight
                + len(category_matches) * self.policy.category_weight
                + len(description_matches) * self.policy.description_weight
                + len(positive_matches) * self.policy.positive_weight
            )
            minimum = track.minimum_score or self.policy.minimum_score
            description_evidence = len(set(description_matches + positive_matches))
            credible = bool(title_matches or category_matches)
            credible = credible or description_evidence >= track.minimum_description_matches
            if not credible or score < minimum:
                continue
            matched = [f"title:{term}" for term in title_matches]
            matched.extend(f"category:{term}" for term in category_matches)
            matched.extend(f"description:{term}" for term in description_matches)
            matched.extend(f"positive:{term}" for term in positive_matches)
            candidates.append((score, -order, name, matched, track))

        if not candidates:
            return RelevanceResult(
                excluded=True,
                exclusion_type="no_track_match",
                location_tier=location_tier,
                location_reason=location_reason,
                reason=(
                    "No enabled career track had a meaningful title/category match or "
                    "enough distinct description evidence."
                ),
            )

        score, _, name, matched_terms, track = max(candidates, key=lambda item: (item[0], item[1]))
        track_exclusions = self._matches(track.exclusion_terms, title)
        if track_exclusions:
            return RelevanceResult(
                matched_track=name,
                score=score,
                matched_terms=matched_terms,
                excluded=True,
                exclusion_type="track_exclusion",
                exclusion_terms=track_exclusions,
                location_tier=location_tier,
                location_reason=location_reason,
                reason=f"Matched {name}, but title matched track exclusion terms: "
                + ", ".join(track_exclusions),
            )

        seniority_level = normalize_text_key(job.seniority.level if job.seniority else "")
        excluded_levels = {
            normalize_text_key(level) for level in self.policy.excluded_seniority_levels
        }
        allowed_levels = {
            normalize_text_key(level) for level in track.allow_seniority_levels
        }
        if seniority_level and seniority_level in excluded_levels and seniority_level not in allowed_levels:
            return RelevanceResult(
                matched_track=name,
                score=score,
                matched_terms=matched_terms,
                excluded=True,
                exclusion_type="advanced_seniority",
                exclusion_terms=[seniority_level],
                location_tier=location_tier,
                location_reason=location_reason,
                reason=(
                    f"Matched {name}, but Phase 1 normalized seniority '{seniority_level}' "
                    "is excluded from the primary discovery queue."
                ),
            )

        strength = "strong" if any(term.startswith("title:") for term in matched_terms) or score >= self.policy.strong_score else "moderate"
        return RelevanceResult(
            matched_track=name,
            strength=strength,
            score=score,
            matched_terms=matched_terms,
            excluded=False,
            exclusion_type="",
            location_tier=location_tier,
            location_reason=location_reason,
            reason=f"Matched {name} with {strength} deterministic evidence (score {score}).",
        )

    def _location(self, job: SourceJob) -> tuple[str, bool, str]:
        policy = self.location_policy
        if not policy.enabled:
            return "not_assessed", False, ""
        text = normalize_text_key(f"{job.location} {job.work_mode}")
        if not text:
            return "unknown", False, "Country is UNKNOWN because location/work mode is empty."
        excluded = self._matches(policy.exclusion_terms, text)
        if excluded:
            return "excluded", True, "Location matched exclusion terms: " + ", ".join(excluded)

        country, country_reason = _determine_country(job, policy)
        allowed_countries = {
            _normalize_country(value)
            for value in policy.allowed_countries
            if _normalize_country(value)
        }
        if not country:
            return "unknown", False, country_reason

        if country != "US":
            if country in allowed_countries:
                return (
                    "international",
                    False,
                    f"Country {country} is international and explicitly allowed. {country_reason}",
                )
            return (
                "international",
                True,
                f"Country {country} is outside allowed_countries. {country_reason}",
            )

        if country not in allowed_countries:
            return (
                "outside_policy",
                True,
                f"United States is outside allowed_countries. {country_reason}",
            )
        if self._matches(policy.remote_terms, text):
            return "remote_us", False, f"Matched configured U.S. remote evidence. {country_reason}"
        if self._matches(policy.primary_markets, text):
            return "local_acceptable", False, f"Matched a configured local market. {country_reason}"
        if self._matches(policy.relocation_markets, text):
            return "relocation_allowed", False, f"Matched a configured U.S. relocation market. {country_reason}"
        if policy.allow_other_locations:
            return "other_us_reviewable", False, f"Confirmed U.S. location is reviewable. {country_reason}"
        return (
            "outside_policy",
            True,
            f"Confirmed U.S. location did not match an allowed market. {country_reason}",
        )

    def _meaningful_matches(self, terms: list[str], text: str) -> list[str]:
        return [
            term
            for term in self._matches(terms, text)
            if normalize_text_key(term) not in self.generic_terms
        ]

    @staticmethod
    def _matches(terms: list[str], text: str) -> list[str]:
        padded = f" {text} "
        matches: list[str] = []
        for term in terms:
            normalized = normalize_text_key(term)
            if normalized and f" {normalized} " in padded:
                matches.append(str(term).strip())
        return list(dict.fromkeys(matches))


def career_track_from_mapping(name: str, value: Mapping[str, Any]) -> CareerTrack:
    return CareerTrack(
        name=name,
        enabled=value.get("enabled", True) is not False,
        aliases=_strings(value.get("aliases")),
        title_terms=_strings(value.get("title_terms")),
        category_terms=_strings(value.get("category_terms")),
        description_terms=_strings(value.get("description_terms")),
        positive_terms=_strings(value.get("positive_terms")),
        exclusion_terms=_strings(value.get("exclusion_terms")),
        minimum_score=_optional_positive_int(value.get("minimum_score")),
        minimum_description_matches=_positive_int(
            value.get("minimum_description_matches", 2),
            f"career_tracks.{name}.minimum_description_matches",
        ),
        allow_seniority_levels=_strings(value.get("allow_seniority_levels")),
    )


def relevance_policy_from_mapping(value: Mapping[str, Any]) -> RelevancePolicy:
    return RelevancePolicy(
        enabled=value.get("enabled", False) is True,
        title_weight=_positive_int(value.get("title_weight", 4), "relevance_policy.title_weight"),
        category_weight=_positive_int(value.get("category_weight", 3), "relevance_policy.category_weight"),
        description_weight=_positive_int(value.get("description_weight", 1), "relevance_policy.description_weight"),
        positive_weight=_positive_int(value.get("positive_weight", 1), "relevance_policy.positive_weight"),
        minimum_score=_positive_int(value.get("minimum_score", 4), "relevance_policy.minimum_score"),
        strong_score=_positive_int(value.get("strong_score", 8), "relevance_policy.strong_score"),
        generic_terms=_strings(value.get("generic_terms")) or list(_DEFAULT_GENERIC_TERMS),
        global_exclusion_terms=_strings(value.get("global_exclusion_terms")),
        excluded_seniority_levels=_strings(value.get("excluded_seniority_levels")),
    )


def location_policy_from_mapping(value: Mapping[str, Any]) -> LocationPolicy:
    return LocationPolicy(
        enabled=value.get("enabled", False) is True,
        primary_markets=_strings(value.get("primary_markets")),
        remote_terms=_strings(value.get("remote_terms")),
        relocation_markets=_strings(value.get("relocation_markets")),
        exclusion_terms=_strings(value.get("exclusion_terms")),
        allow_other_locations=value.get("allow_other_locations", True) is not False,
        allowed_countries=_strings(value.get("allowed_countries")) or ["US"],
    )


def _determine_country(job: SourceJob, policy: LocationPolicy) -> tuple[str, str]:
    explicit = _explicit_metadata_country(job.metadata)
    if explicit:
        return explicit, f"Used explicit ATS country metadata ({explicit})."

    location = str(job.location or "").strip()
    work_mode = str(job.work_mode or "").strip()
    text = normalize_text_key(f"{location} {work_mode}")
    padded = f" {text} "

    for code, terms in _LOCATION_COUNTRY_TERMS.items():
        if code == "US":
            continue
        for term in terms:
            normalized = normalize_text_key(term)
            if normalized and f" {normalized} " in padded:
                return code, f"Location text contains deterministic country evidence '{term}'."

    for term in _LOCATION_COUNTRY_TERMS["US"]:
        normalized = normalize_text_key(term)
        if normalized and f" {normalized} " in padded:
            return "US", f"Location text contains deterministic U.S. evidence '{term}'."

    uppercase_tokens = set(re.findall(r"\b[A-Z]{2}\b", location))
    if "US" in uppercase_tokens:
        return "US", "Location text contains the U.S. country code."
    state_codes = sorted(uppercase_tokens & _US_STATE_CODES)
    if state_codes:
        return "US", f"Location text contains U.S. state code {state_codes[0]}."

    state_names = [
        name for name in _US_STATE_NAMES if f" {name} " in padded
    ]
    if state_names:
        return "US", f"Location text contains U.S. state name '{sorted(state_names)[0]}'."

    matcher = JobRelevanceEvaluator._matches
    if matcher(policy.remote_terms, text):
        return "US", "Matched a privately configured U.S. remote term."
    if matcher(policy.primary_markets, text):
        return "US", "Matched a privately configured local U.S. market."
    if matcher(policy.relocation_markets, text):
        return "US", "Matched a privately configured U.S. relocation market."
    return "", "Country is UNKNOWN because no reliable country evidence was found."


def _explicit_metadata_country(value: Any, *, depth: int = 0) -> str:
    if depth > 5 or value is None:
        return ""
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = re.sub(r"[^a-z]", "", str(key).casefold())
            if key_name in {
                "country",
                "countrycode",
                "countryname",
                "addresscountry",
                "postalcountry",
            }:
                country = _country_from_metadata_value(item)
                if country:
                    return country
        for key, item in value.items():
            if str(key).startswith("_discovery_"):
                continue
            country = _explicit_metadata_country(item, depth=depth + 1)
            if country:
                return country
    elif isinstance(value, (list, tuple)):
        for item in value:
            country = _explicit_metadata_country(item, depth=depth + 1)
            if country:
                return country
    return ""


def _country_from_metadata_value(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("code", "countryCode", "name", "label", "value"):
            if key in value:
                country = _normalize_country(value[key])
                if country:
                    return country
        return ""
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return _normalize_country(value)
    return ""


def _normalize_country(value: Any) -> str:
    normalized = normalize_text_key(str(value or ""))
    if not normalized:
        return ""
    for code, aliases in _COUNTRY_ALIASES.items():
        if normalized == code.casefold() or normalized in aliases:
            return code
    compact = normalized.replace(" ", "")
    if re.fullmatch(r"[a-z]{2,3}", compact):
        return compact.upper()
    return normalized.upper().replace(" ", "_")


def _metadata_text(value: Any, *, depth: int = 0) -> str:
    if depth > 4 or value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(
            _metadata_text(item, depth=depth + 1)
            for key, item in value.items()
            if not str(key).startswith("_discovery_")
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(_metadata_text(item, depth=depth + 1) for item in value)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer.") from exc
    if parsed < 1:
        raise ValueError(f"{field_name} must be positive.")
    return parsed


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, "minimum_score")
