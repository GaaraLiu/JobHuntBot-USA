"""Typed data models shared by the Phase 1 matching pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Recommendation(str, Enum):
    """A fit recommendation, distinct from application lifecycle status."""

    APPLY = "APPLY"
    REVIEW = "REVIEW"
    SKIP = "SKIP"


@dataclass(slots=True)
class ValidationIssue:
    severity: str
    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class Education:
    institution: str = ""
    degree: str = ""
    field_of_study: str = ""
    graduation_date: str = ""
    level: str = ""


@dataclass(slots=True)
class WorkExperience:
    company: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    years: float | None = None
    skills: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(slots=True)
class Project:
    name: str = ""
    description: str = ""
    skills: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DomainExperience:
    first_relevant_year: int | None = None
    current: bool | None = None
    context: str = ""
    evidence_summary: str = ""


@dataclass(slots=True)
class WorkAuthorization:
    country: str = ""
    current_authorization: str = ""
    authorized_countries: list[str] = field(default_factory=list)
    requires_sponsorship_now: bool | None = None
    requires_sponsorship_in_future: bool | None = None
    citizenships: list[str] = field(default_factory=list)
    citizenships_complete: bool = False
    security_clearances: list[str] = field(default_factory=list)
    security_clearances_complete: bool = False
    licenses_certifications: list[str] = field(default_factory=list)
    licenses_certifications_complete: bool = False
    notes: str = ""


@dataclass(slots=True)
class CandidateProfile:
    schema_version: int = 1
    candidate: dict[str, Any] = field(default_factory=dict)
    education: list[Education] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    work_experience: list[WorkExperience] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    preferred_roles: list[str] = field(default_factory=list)
    secondary_roles: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=list)
    remote_preferences: list[str] = field(default_factory=list)
    minimum_salary: float | None = None
    salary_currency: str = "USD"
    salary_period: str = "year"
    work_authorization: WorkAuthorization = field(default_factory=WorkAuthorization)
    years_of_experience: float | None = None
    domain_experience: dict[str, DomainExperience] = field(default_factory=dict)
    industries: list[str] = field(default_factory=list)
    required_constraints: dict[str, Any] = field(default_factory=dict)
    excluded_roles: list[str] = field(default_factory=list)
    excluded_locations: list[str] = field(default_factory=list)
    excluded_conditions: list[str] = field(default_factory=list)
    resume_routing_file: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def all_skills(self) -> list[str]:
        values = list(self.skills)
        for experience in self.work_experience:
            values.extend(experience.skills)
        for project in self.projects:
            values.extend(project.skills)
        return list(dict.fromkeys(value for value in values if value))

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("raw", None)
        return result


@dataclass(slots=True)
class SalaryRange:
    minimum: float | None = None
    maximum: float | None = None
    currency: str = "USD"
    period: str = "year"
    raw_text: str = ""


@dataclass(slots=True)
class ExperienceRequirement:
    minimum_years: float | None = None
    maximum_years: float | None = None
    raw_text: str = ""


@dataclass(slots=True)
class Seniority:
    level: str = ""
    evidence: str = ""


@dataclass(slots=True)
class RawJob:
    title: str
    company: str
    job_description: str
    location: str = ""
    source: str = "manual"
    source_native_id: str = ""
    source_url: str = ""
    apply_url: str = ""
    posted_date: str = ""
    discovered_date: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedJob:
    job_id: str
    title: str
    company: str
    location: str
    source: str
    source_url: str
    apply_url: str
    job_description: str
    salary: SalaryRange | None = None
    employment_type: str = ""
    experience_required: ExperienceRequirement | None = None
    seniority: Seniority | None = None
    education_required: list[str] = field(default_factory=list)
    skills_required: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    posted_date: str = ""
    discovered_date: str = ""
    source_native_id: str = ""
    job_family: str = ""
    industries: list[str] = field(default_factory=list)
    remote_policy: str = ""
    licenses_required: list[str] = field(default_factory=list)
    security_clearance_required: list[str] = field(default_factory=list)
    work_authorization_requirements: list[str] = field(default_factory=list)
    parser_evidence: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScoreComponent:
    component: str
    maximum_points: float
    awarded_points: float | None
    reason: str
    evidence: list[str] = field(default_factory=list)
    status: str = "assessed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SeniorityExperienceRisk:
    triggered: bool = False
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExperienceRequirementRisk:
    triggered: bool = False
    reason: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScoreResult:
    overall_score: float
    coverage: float
    recommendation: Recommendation
    components: list[ScoreComponent] = field(default_factory=list)
    matched_requirements: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    unknown_requirements: list[str] = field(default_factory=list)
    hard_blockers: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    seniority_experience_risk: SeniorityExperienceRisk = field(
        default_factory=SeniorityExperienceRisk
    )
    experience_requirement_risk: ExperienceRequirementRisk = field(
        default_factory=ExperienceRequirementRisk
    )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["recommendation"] = self.recommendation.value
        return result


@dataclass(slots=True)
class ResumeRecommendation:
    resume_id: str = ""
    file_path: str = ""
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)

    @property
    def selected(self) -> bool:
        return bool(self.resume_id and self.file_path)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["selected"] = self.selected
        return result


@dataclass(slots=True)
class ResumeRoute:
    resume_id: str
    file_path: str
    target_job_families: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    priority: int = 0
    active: bool = True


@dataclass(slots=True)
class ResumeRoutingConfig:
    schema_version: int = 1
    default_resume_id: str = ""
    resumes: list[ResumeRoute] = field(default_factory=list)


@dataclass(slots=True)
class PipelineResult:
    job: NormalizedJob
    score: ScoreResult
    resume: ResumeRecommendation
    saved: bool = False
    storage_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "score": self.score.to_dict(),
            "resume": self.resume.to_dict(),
            "saved": self.saved,
            "storage_action": self.storage_action,
        }
