"""Structured, deterministic resume routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CandidateProfile,
    NormalizedJob,
    ResumeRecommendation,
    ResumeRoute,
    ResumeRoutingConfig,
)
from .normalization import normalize_skills, normalize_text_key


class ResumeRoutingError(ValueError):
    """Raised when structured resume-routing JSON is invalid."""


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def load_resume_routing(path: str | Path) -> ResumeRoutingConfig:
    route_path = Path(path)
    try:
        with route_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ResumeRoutingError(f"Could not load resume routing {route_path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ResumeRoutingError("Resume-routing root must be a JSON object")
    if not isinstance(value.get("resumes"), list):
        raise ResumeRoutingError("Resume-routing JSON must contain a resumes array")

    routes: list[ResumeRoute] = []
    seen_ids: set[str] = set()
    for index, raw_route in enumerate(value["resumes"]):
        if not isinstance(raw_route, Mapping):
            raise ResumeRoutingError(f"Resume route at index {index} must be an object")
        resume_id = str(raw_route.get("resume_id", "")).strip()
        file_path = str(raw_route.get("file_path", "")).strip()
        if not resume_id or not file_path:
            raise ResumeRoutingError(f"Resume route at index {index} requires resume_id and file_path")
        if resume_id in seen_ids:
            raise ResumeRoutingError(f"Duplicate resume_id: {resume_id}")
        seen_ids.add(resume_id)
        priority_value = raw_route.get("priority", 0)
        if isinstance(priority_value, bool):
            raise ResumeRoutingError(f"priority must be an integer for {resume_id}")
        try:
            priority = int(priority_value)
        except (TypeError, ValueError) as exc:
            raise ResumeRoutingError(f"priority must be an integer for {resume_id}") from exc
        routes.append(
            ResumeRoute(
                resume_id=resume_id,
                file_path=file_path,
                target_job_families=_strings(raw_route.get("target_job_families")),
                keywords=_strings(raw_route.get("keywords")),
                skills=_strings(raw_route.get("skills")),
                industries=_strings(raw_route.get("industries")),
                priority=priority,
                active=raw_route.get("active", True) is not False,
            )
        )

    default_id = str(value.get("default_resume_id", "")).strip()
    if default_id and default_id not in seen_ids:
        raise ResumeRoutingError(f"default_resume_id does not exist: {default_id}")
    return ResumeRoutingConfig(
        schema_version=int(value.get("schema_version", 1) or 1),
        default_resume_id=default_id,
        resumes=routes,
    )


@dataclass(slots=True)
class _RouteScore:
    route: ResumeRoute
    score: float
    evidence: list[str]


class ResumeRouter:
    """Select a resume from structured rules and explain the selection."""

    def __init__(self, routing: ResumeRoutingConfig, skill_aliases: dict[str, str] | None = None):
        self.routing = routing
        self.skill_aliases = skill_aliases or {}

    def route(self, profile: CandidateProfile, job: NormalizedJob) -> ResumeRecommendation:
        candidates = [self._score_route(route, profile, job) for route in self.routing.resumes if route.active]
        candidates.sort(key=lambda item: (-item.score, -item.route.priority, item.route.resume_id))
        positive = [item for item in candidates if item.score > 0]

        selected: _RouteScore | None = positive[0] if positive else None
        used_default = False
        if selected is None and self.routing.default_resume_id:
            selected = next(
                (item for item in candidates if item.route.resume_id == self.routing.default_resume_id),
                None,
            )
            used_default = selected is not None

        if selected is None:
            return ResumeRecommendation(
                reason="No structured resume route matched, and no valid default resume is configured.",
                evidence=[],
                alternatives=[item.route.resume_id for item in candidates[:3]],
            )

        alternatives = [
            f"{item.route.resume_id} ({item.score:.1f})"
            for item in candidates
            if item.route.resume_id != selected.route.resume_id
        ][:3]
        if used_default:
            reason = f"Selected configured default resume '{selected.route.resume_id}' because no route had positive matching evidence."
        else:
            reason = (
                f"Selected '{selected.route.resume_id}' with routing score {selected.score:.1f}/100 "
                "from structured job-family, keyword, skill, and industry evidence."
            )
        return ResumeRecommendation(
            resume_id=selected.route.resume_id,
            file_path=selected.route.file_path,
            reason=reason,
            evidence=selected.evidence,
            alternatives=alternatives,
        )

    def _score_route(self, route: ResumeRoute, profile: CandidateProfile, job: NormalizedJob) -> _RouteScore:
        score = 0.0
        evidence: list[str] = []
        job_family = normalize_text_key(job.job_family)
        target_families = {normalize_text_key(item) for item in route.target_job_families}
        if job_family and job_family in target_families:
            score += 40
            evidence.append(f"Job family match: {job.job_family}")

        job_blob = normalize_text_key(f"{job.title}\n{job.job_description}")
        keyword_hits = [keyword for keyword in route.keywords if normalize_text_key(keyword) in job_blob]
        if route.keywords:
            keyword_points = 25 * len(keyword_hits) / len(route.keywords)
            score += keyword_points
            if keyword_hits:
                evidence.append(f"Keyword matches: {', '.join(keyword_hits)}")

        candidate_skills = set(normalize_skills(profile.all_skills, self.skill_aliases))
        job_skills = set(normalize_skills(job.skills_required + job.preferred_skills, self.skill_aliases))
        route_skills = set(normalize_skills(route.skills, self.skill_aliases))
        confirmed_route_skills = route_skills & candidate_skills
        skill_hits = sorted(confirmed_route_skills & job_skills)
        if route_skills:
            skill_points = 25 * len(skill_hits) / len(route_skills)
            score += skill_points
            if skill_hits:
                evidence.append(f"Candidate-confirmed resume skills matching the job: {', '.join(skill_hits)}")

        route_industries = {normalize_text_key(item) for item in route.industries}
        job_industries = {normalize_text_key(item) for item in job.industries}
        industry_hits = sorted(route_industries & job_industries)
        if industry_hits:
            score += 10
            evidence.append(f"Industry matches: {', '.join(industry_hits)}")

        return _RouteScore(route=route, score=round(min(score, 100.0), 1), evidence=evidence)

