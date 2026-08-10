"""Executable core for the JobHuntBot workflow."""

from .models import (
    CandidateProfile,
    NormalizedJob,
    PipelineResult,
    Recommendation,
    ResumeRecommendation,
    ScoreComponent,
    ScoreResult,
    ValidationIssue,
    ValidationResult,
)

__all__ = [
    "CandidateProfile",
    "NormalizedJob",
    "PipelineResult",
    "Recommendation",
    "ResumeRecommendation",
    "ScoreComponent",
    "ScoreResult",
    "ValidationIssue",
    "ValidationResult",
]

__version__ = "0.1.0"

