"""End-to-end Phase 1 job-matching pipeline."""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .job_parser import DeterministicJobParser, JobParser
from .models import CandidateProfile, PipelineResult, RawJob, Recommendation
from .normalization import validate_normalized_job
from .profile import validate_candidate_profile
from .resume_router import ResumeRouter
from .scoring import JobFitScorer
from .storage import DashboardSchema, save_job_result


class PipelineValidationError(ValueError):
    """Raised when critical supplied inputs are incomplete or invalid."""


class JobHuntPipeline:
    def __init__(
        self,
        *,
        profile: CandidateProfile,
        config: AppConfig,
        resume_router: ResumeRouter,
        parser: JobParser | None = None,
        schema: DashboardSchema | None = None,
    ):
        self.profile = profile
        self.config = config
        self.resume_router = resume_router
        self.parser = parser or DeterministicJobParser(config)
        self.scorer = JobFitScorer(config)
        self.schema = schema

    def analyze(self, raw_job: RawJob, save_path: str | Path | None = None) -> PipelineResult:
        profile_validation = validate_candidate_profile(self.profile)
        if not profile_validation.valid:
            details = "; ".join(f"{issue.field}: {issue.message}" for issue in profile_validation.errors)
            raise PipelineValidationError(f"Candidate profile has critical validation errors: {details}")
        missing_job_fields = [
            name
            for name, value in (
                ("title", raw_job.title),
                ("company", raw_job.company),
                ("job_description", raw_job.job_description),
            )
            if not str(value or "").strip()
        ]
        if missing_job_fields:
            raise PipelineValidationError(
                "Raw job is missing required fields: " + ", ".join(missing_job_fields)
            )

        job = self.parser.parse(raw_job)
        job_validation = validate_normalized_job(job)
        if not job_validation.valid:
            details = "; ".join(f"{issue.field}: {issue.message}" for issue in job_validation.errors)
            raise PipelineValidationError(f"Normalized job has critical validation errors: {details}")
        score = self.scorer.score(self.profile, job)
        resume = self.resume_router.route(self.profile, job)
        if not resume.selected and score.recommendation == Recommendation.APPLY:
            score.recommendation = Recommendation.REVIEW
            score.unknown_requirements.append("Resume routing: no structured resume route could be selected.")
            score.reasoning.append("The recommendation is capped at REVIEW because no resume was selected.")

        result = PipelineResult(job=job, score=score, resume=resume)
        if save_path is not None:
            result.storage_action = save_job_result(save_path, result, self.schema)
            result.saved = True
        return result
