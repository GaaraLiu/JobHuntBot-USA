"""Candidate profile loading, compatibility mapping, and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CandidateProfile,
    Education,
    Project,
    ValidationIssue,
    ValidationResult,
    WorkAuthorization,
    WorkExperience,
)


class ProfileLoadError(ValueError):
    """Raised when a candidate profile cannot be decoded safely."""


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_education(items: Any) -> list[Education]:
    result: list[Education] = []
    if not isinstance(items, list):
        return result
    for item in items:
        value = _mapping(item)
        result.append(
            Education(
                institution=str(value.get("institution", "")).strip(),
                degree=str(value.get("degree", "")).strip(),
                field_of_study=str(value.get("field_of_study", "")).strip(),
                graduation_date=str(value.get("graduation_date", "")).strip(),
                level=str(value.get("level", "")).strip(),
            )
        )
    return result


def _parse_experience(items: Any) -> list[WorkExperience]:
    result: list[WorkExperience] = []
    if not isinstance(items, list):
        return result
    for item in items:
        value = _mapping(item)
        result.append(
            WorkExperience(
                company=str(value.get("company", "")).strip(),
                title=str(value.get("title", "")).strip(),
                start_date=str(value.get("start_date", "")).strip(),
                end_date=str(value.get("end_date", "")).strip(),
                years=_optional_float(value.get("years")),
                skills=_string_list(value.get("skills")),
                industries=_string_list(value.get("industries")),
                description=str(value.get("description", "")).strip(),
            )
        )
    return result


def _parse_projects(items: Any) -> list[Project]:
    result: list[Project] = []
    if not isinstance(items, list):
        return result
    for item in items:
        value = _mapping(item)
        result.append(
            Project(
                name=str(value.get("name", "")).strip(),
                description=str(value.get("description", "")).strip(),
                skills=_string_list(value.get("skills")),
                industries=_string_list(value.get("industries")),
            )
        )
    return result


def parse_candidate_profile(value: Mapping[str, Any]) -> CandidateProfile:
    """Parse current or legacy profile JSON without filling missing facts."""

    targets = _mapping(value.get("targets"))
    preferences = _mapping(value.get("preferences"))
    compensation = _mapping(value.get("compensation"))
    constraints = _mapping(value.get("constraints"))
    authorization = _mapping(value.get("work_authorization"))

    preferred_roles = _string_list(preferences.get("preferred_roles"))
    if not preferred_roles:
        preferred_roles = _string_list(targets.get("primary_role_families"))
    secondary_roles = _string_list(preferences.get("secondary_roles"))
    if not secondary_roles:
        secondary_roles = _string_list(targets.get("secondary_role_families"))
    preferred_locations = _string_list(preferences.get("preferred_locations"))
    if not preferred_locations:
        preferred_locations = _string_list(targets.get("target_locations"))
    industries = _string_list(preferences.get("industries"))
    if not industries:
        industries = _string_list(targets.get("target_industries"))

    remote_preferences = _string_list(preferences.get("remote_preferences"))
    if not remote_preferences:
        remote_preferences = _string_list(targets.get("remote_hybrid_onsite_preference"))

    excluded_roles = _string_list(constraints.get("excluded_roles"))
    if not excluded_roles:
        excluded_roles = _string_list(targets.get("roles_to_avoid"))

    minimum_salary = _optional_float(compensation.get("minimum_salary"))
    years_of_experience = _optional_float(value.get("years_of_experience"))

    return CandidateProfile(
        schema_version=int(value.get("schema_version", 1) or 1),
        candidate=_mapping(value.get("candidate")),
        education=_parse_education(value.get("education")),
        skills=_string_list(value.get("skills")),
        work_experience=_parse_experience(value.get("work_experience")),
        projects=_parse_projects(value.get("projects")),
        preferred_roles=preferred_roles,
        secondary_roles=secondary_roles,
        preferred_locations=preferred_locations,
        remote_preferences=remote_preferences,
        minimum_salary=minimum_salary,
        salary_currency=str(compensation.get("currency", "USD") or "USD").strip().upper(),
        salary_period=str(compensation.get("period", "year") or "year").strip().lower(),
        work_authorization=WorkAuthorization(
            country=str(authorization.get("country", "")).strip(),
            current_authorization=str(authorization.get("current_authorization", "")).strip(),
            authorized_countries=_string_list(authorization.get("authorized_countries")),
            requires_sponsorship_now=authorization.get("requires_sponsorship_now")
            if isinstance(authorization.get("requires_sponsorship_now"), bool)
            else None,
            requires_sponsorship_in_future=authorization.get("requires_sponsorship_in_future")
            if isinstance(authorization.get("requires_sponsorship_in_future"), bool)
            else None,
            citizenships=_string_list(authorization.get("citizenships")),
            citizenships_complete=authorization.get("citizenships_complete") is True,
            security_clearances=_string_list(authorization.get("security_clearances")),
            security_clearances_complete=authorization.get("security_clearances_complete") is True,
            licenses_certifications=_string_list(authorization.get("licenses_certifications")),
            licenses_certifications_complete=authorization.get("licenses_certifications_complete") is True,
            notes=str(authorization.get("notes", authorization.get("notes_for_unclear_forms", ""))).strip(),
        ),
        years_of_experience=years_of_experience,
        industries=industries,
        required_constraints=_mapping(constraints.get("required")),
        excluded_roles=excluded_roles,
        excluded_locations=_string_list(constraints.get("excluded_locations")),
        excluded_conditions=_string_list(constraints.get("excluded_conditions")),
        resume_routing_file=str(value.get("resume_routing_file", "")).strip(),
        raw=dict(value),
    )


def load_candidate_profile(path: str | Path) -> CandidateProfile:
    profile_path = Path(path)
    try:
        with profile_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileLoadError(f"Could not load candidate profile {profile_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileLoadError("Candidate profile root must be a JSON object")
    try:
        return parse_candidate_profile(value)
    except (TypeError, ValueError) as exc:
        raise ProfileLoadError(f"Invalid candidate profile structure: {exc}") from exc


def validate_candidate_profile(profile: CandidateProfile) -> ValidationResult:
    """Return explicit errors and warnings; never synthesize missing values."""

    issues: list[ValidationIssue] = []

    def add(severity: str, field_name: str, code: str, message: str) -> None:
        issues.append(ValidationIssue(severity, field_name, code, message))

    if profile.schema_version < 1:
        add("error", "schema_version", "invalid_schema_version", "schema_version must be at least 1.")
    if not profile.preferred_roles:
        add("error", "preferences.preferred_roles", "missing_preferred_roles", "At least one preferred role is required for title-fit scoring.")
    if not profile.skills:
        add("error", "skills", "missing_skills", "A structured candidate skill list is required for reliable skill-fit scoring.")
    if profile.years_of_experience is None:
        add("error", "years_of_experience", "missing_years_of_experience", "Years of experience must be supplied; it will not be inferred.")
    elif profile.years_of_experience < 0:
        add("error", "years_of_experience", "invalid_years_of_experience", "Years of experience cannot be negative.")
    if not profile.work_authorization.country:
        add("error", "work_authorization.country", "missing_authorization_country", "Work-authorization country is required to evaluate explicit restrictions.")
    if not profile.work_authorization.current_authorization:
        add("error", "work_authorization.current_authorization", "missing_current_authorization", "Current work authorization must be supplied; it will not be guessed.")
    if not profile.resume_routing_file:
        add("error", "resume_routing_file", "missing_resume_routing_file", "A structured resume-routing JSON file is required for a resume recommendation.")

    if not profile.preferred_locations and not profile.remote_preferences:
        add("warning", "preferences.preferred_locations", "missing_location_preferences", "Location fit will be unknown until location or remote preferences are supplied.")
    if profile.minimum_salary is None:
        add("warning", "compensation.minimum_salary", "missing_minimum_salary", "Salary fit will be unknown; no salary preference will be inferred.")
    elif profile.minimum_salary < 0:
        add("error", "compensation.minimum_salary", "invalid_minimum_salary", "Minimum salary cannot be negative.")
    if not profile.education:
        add("warning", "education", "missing_education", "Education fit may be unknown when a job states a degree requirement.")
    if not profile.industries:
        add("warning", "preferences.industries", "missing_industries", "Industry fit will be unknown until preferred industries are supplied.")
    if not profile.work_experience and not profile.projects:
        add("warning", "work_experience", "missing_experience_evidence", "No structured work experience or projects were supplied as supporting evidence.")

    raw_authorization = _mapping(profile.raw.get("work_authorization"))
    for name in (
        "requires_sponsorship_now",
        "requires_sponsorship_in_future",
        "citizenships_complete",
        "security_clearances_complete",
        "licenses_certifications_complete",
    ):
        raw_value = raw_authorization.get(name)
        if raw_value is not None and not isinstance(raw_value, bool):
            add("error", f"work_authorization.{name}", "invalid_boolean", f"{name} must be true, false, or null.")

    return ValidationResult(issues)
