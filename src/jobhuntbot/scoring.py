"""Conservative, evidence-backed fit scoring and hard-blocker evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import AppConfig
from .models import (
    CandidateProfile,
    ExperienceRequirementRisk,
    NormalizedJob,
    Recommendation,
    ScoreComponent,
    ScoreResult,
    SeniorityExperienceRisk,
)
from .normalization import normalize_skill, normalize_skills, normalize_text_key


_EDUCATION_RANKS = {
    "high school": 1,
    "associate": 2,
    "bachelor": 3,
    "master": 4,
    "mba": 4,
    "phd": 5,
    "doctorate": 5,
}


_SENIOR_OR_HIGHER = {
    "senior",
    "lead",
    "staff",
    "principal",
    "manager",
    "director",
    "vp",
    "executive",
}

_EXPERIENCE_RISK_MINIMUM_YEARS = 5
_EXPLICIT_EXPERIENCE_CONTEXT_RE = re.compile(
    r"\b(?:experience|experienced|minimum|require(?:d|ment|ments)?|qualification|qualifications)\b",
    re.IGNORECASE,
)


def _location_keys(value: str) -> set[str]:
    key = normalize_text_key(value)
    if not key:
        return set()
    values = {key}
    conventional_nyc = re.match(
        r"^(?:new york(?: city)?(?: ny)?|nyc|manhattan(?: ny)?)(?:\b|$)",
        key,
    )
    ats_nyc = re.match(
        r"^(?:(?:united states|usa|us) )?ny (?:new york(?: city)?|nyc|manhattan)(?:\b|$)",
        key,
    )
    if conventional_nyc or ats_nyc:
        values.add("new york city metro")
    return values


@dataclass(slots=True)
class _ScoreContext:
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _tokens(value: str) -> set[str]:
    return {token for token in normalize_text_key(value).split() if token}


def _credential_key(value: str) -> str:
    key = normalize_text_key(value)
    key = re.sub(r"\b(?:valid|required|license|licensed|certification|certified)\b", " ", key)
    return " ".join(key.split())


def _contains_phrase(haystack: str, needle: str) -> bool:
    return bool(needle and normalize_text_key(needle) in normalize_text_key(haystack))


class JobFitScorer:
    def __init__(self, config: AppConfig):
        self.config = config
        self.aliases = dict(config.normalization.get("skill_aliases", {}))

    def score(self, profile: CandidateProfile, job: NormalizedJob) -> ScoreResult:
        context = _ScoreContext()
        components = [
            self._score_role(profile, job, context),
            self._score_skills(profile, job, context),
            self._score_education(profile, job, context),
            self._score_experience(profile, job, context),
            self._score_industry(profile, job, context),
            self._score_location(profile, job, context),
            self._score_salary(profile, job, context),
        ]
        blockers, blocker_unknowns, blocker_matches = self._hard_requirements(profile, job)
        context.unknown.extend(blocker_unknowns)
        context.matched.extend(blocker_matches)
        seniority_risk = self._seniority_experience_risk(profile, job)
        experience_risk = self._experience_requirement_risk(profile, job)

        assessed = [item for item in components if item.awarded_points is not None]
        assessed_max = sum(item.maximum_points for item in assessed)
        awarded = sum(item.awarded_points or 0 for item in assessed)
        total_max = sum(item.maximum_points for item in components)
        overall = round((awarded / assessed_max * 100) if assessed_max else 0.0, 1)
        coverage = round((assessed_max / total_max) if total_max else 0.0, 3)

        thresholds = self.config.thresholds
        critical_unknown = bool(blocker_unknowns)
        if blockers:
            recommendation = Recommendation.SKIP
        elif overall >= thresholds.apply_min:
            recommendation = (
                Recommendation.APPLY
                if coverage >= thresholds.minimum_apply_coverage and not critical_unknown
                else Recommendation.REVIEW
            )
        elif overall >= thresholds.review_min:
            recommendation = Recommendation.REVIEW
        else:
            recommendation = Recommendation.SKIP

        threshold_recommendation = recommendation
        if (
            seniority_risk.triggered or experience_risk.triggered
        ) and recommendation == Recommendation.APPLY:
            recommendation = Recommendation.REVIEW

        reasoning = [
            f"Evidence-backed fit score {overall:.1f}/100 with {coverage:.0%} scoring coverage.",
            (
                f"Configured thresholds: APPLY >= {thresholds.apply_min:g}, "
                f"REVIEW >= {thresholds.review_min:g}."
            ),
        ]
        if blockers:
            reasoning.append("One or more explicit hard blockers override the numeric score.")
        elif critical_unknown:
            reasoning.append("A critical hard-requirement fact is unknown, so APPLY is capped at REVIEW.")
        elif coverage < thresholds.minimum_apply_coverage and overall >= thresholds.apply_min:
            reasoning.append("Evidence coverage is below the configured APPLY minimum, so human review is required.")
        else:
            reasoning.append(
                f"The evidence and configured thresholds produce {threshold_recommendation.value}."
            )
        if seniority_risk.triggered:
            if threshold_recommendation == Recommendation.APPLY:
                reasoning.append(
                    "The seniority/experience safeguard caps APPLY at REVIEW because the "
                    "role explicitly signals senior-or-higher seniority, requires at least "
                    "5 years of experience, and the candidate does not demonstrate that level."
                )
            else:
                reasoning.append(
                    "The seniority/experience safeguard is triggered; the existing "
                    f"{recommendation.value} decision remains unchanged."
                )
        if experience_risk.triggered:
            if threshold_recommendation == Recommendation.APPLY:
                reasoning.append(
                    "The explicit experience-requirement safeguard caps APPLY at REVIEW "
                    "because the role requires at least 5 years and the candidate does not "
                    "demonstrate the stated minimum."
                )
            else:
                reasoning.append(
                    "The explicit experience-requirement safeguard is triggered; the existing "
                    f"{recommendation.value} decision remains unchanged."
                )

        return ScoreResult(
            overall_score=overall,
            coverage=coverage,
            recommendation=recommendation,
            components=components,
            matched_requirements=list(dict.fromkeys(context.matched)),
            missing_requirements=list(dict.fromkeys(context.missing)),
            unknown_requirements=list(dict.fromkeys(context.unknown)),
            hard_blockers=list(dict.fromkeys(blockers)),
            reasoning=reasoning,
            seniority_experience_risk=seniority_risk,
            experience_requirement_risk=experience_risk,
        )

    def _experience_requirement_risk(
        self,
        profile: CandidateProfile,
        job: NormalizedJob,
    ) -> ExperienceRequirementRisk:
        requirement = job.experience_required
        if (
            requirement is None
            or requirement.minimum_years is None
            or requirement.minimum_years < _EXPERIENCE_RISK_MINIMUM_YEARS
        ):
            return ExperienceRequirementRisk(
                reason="The job does not state a minimum experience requirement of at least 5 years."
            )

        raw = requirement.raw_text.strip()
        evidence_line = next(
            (
                line.strip()
                for line in job.job_description.splitlines()
                if raw
                and normalize_text_key(raw) in normalize_text_key(line)
                and _EXPLICIT_EXPERIENCE_CONTEXT_RE.search(line)
            ),
            "",
        )
        if not evidence_line:
            return ExperienceRequirementRisk(
                reason="The extracted years value lacks reliable explicit experience-requirement context.",
                evidence=[raw] if raw else [],
            )

        required = requirement.minimum_years
        evidence = [f"Minimum experience: {evidence_line}"]
        actual = profile.years_of_experience
        if actual is None:
            evidence.append("Candidate years of experience: UNKNOWN")
            return ExperienceRequirementRisk(
                triggered=True,
                reason=(
                    f"Role explicitly requires at least {required:g} years of experience, while "
                    "candidate total professional experience is UNKNOWN."
                ),
                evidence=evidence,
            )
        evidence.append(f"Candidate years of experience: {actual:g}")
        if actual < required:
            return ExperienceRequirementRisk(
                triggered=True,
                reason=(
                    f"Role explicitly requires at least {required:g} years of experience, while "
                    f"the candidate reports {actual:g} years."
                ),
                evidence=evidence,
            )
        return ExperienceRequirementRisk(
            reason="Candidate-reported experience meets the explicit minimum.",
            evidence=evidence,
        )

    def _seniority_experience_risk(
        self,
        profile: CandidateProfile,
        job: NormalizedJob,
    ) -> SeniorityExperienceRisk:
        seniority = job.seniority
        requirement = job.experience_required
        if seniority is None or seniority.level not in _SENIOR_OR_HIGHER:
            return SeniorityExperienceRisk(
                reason="No explicit senior-or-higher job seniority was detected."
            )
        if requirement is None or requirement.minimum_years is None or requirement.minimum_years < 5:
            return SeniorityExperienceRisk(
                reason="The job does not state a minimum experience requirement of at least 5 years.",
                evidence=[f"Seniority: {seniority.level} ({seniority.evidence})"],
            )

        required = requirement.minimum_years
        minimum_evidence = requirement.raw_text or f"{required:g} years"
        evidence = [
            f"Seniority: {seniority.level} ({seniority.evidence})",
            f"Minimum experience: {minimum_evidence}",
        ]
        actual = profile.years_of_experience
        if actual is None:
            evidence.append("Candidate years of experience: UNKNOWN")
            return SeniorityExperienceRisk(
                triggered=True,
                reason=(
                    f"Role explicitly signals {seniority.level} seniority and requires at least "
                    f"{required:g} years of experience, while candidate experience is UNKNOWN "
                    "and not evidenced at that level."
                ),
                evidence=evidence,
            )
        if actual < required:
            evidence.append(f"Candidate years of experience: {actual:g}")
            return SeniorityExperienceRisk(
                triggered=True,
                reason=(
                    f"Role explicitly signals {seniority.level} seniority and requires at least "
                    f"{required:g} years of experience, while the candidate reports {actual:g} years."
                ),
                evidence=evidence,
            )
        evidence.append(f"Candidate years of experience: {actual:g}")
        return SeniorityExperienceRisk(
            reason="Candidate-reported experience meets the explicit minimum for this senior role.",
            evidence=evidence,
        )

    def _component(
        self,
        name: str,
        points: float | None,
        reason: str,
        evidence: list[str],
    ) -> ScoreComponent:
        maximum = self.config.scoring_weights[name]
        if points is None:
            return ScoreComponent(name, maximum, None, reason, evidence, "unknown")
        return ScoreComponent(name, maximum, round(max(0.0, min(maximum, points)), 1), reason, evidence)

    def _score_role(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["role_title"]
        roles = profile.preferred_roles + profile.secondary_roles
        if not roles:
            context.unknown.append("Role fit: candidate preferred roles are not supplied.")
            return self._component("role_title", None, "Candidate role preferences are unknown.", [])
        if not job.title:
            context.unknown.append("Role fit: job title is not supplied.")
            return self._component("role_title", None, "The job title is unknown.", [])

        job_title = normalize_text_key(job.title)
        job_tokens = _tokens(job.title)
        best_score = 0.0
        best_role = ""
        for index, role in enumerate(roles):
            role_key = normalize_text_key(role)
            role_tokens = _tokens(role)
            if role_key and role_key in job_title:
                similarity = 1.0
            else:
                union = role_tokens | job_tokens
                jaccard = len(role_tokens & job_tokens) / len(union) if union else 0.0
                similarity = 0.4 + 0.6 * jaccard if role_tokens & job_tokens else 0.0
            if index >= len(profile.preferred_roles):
                similarity *= 0.85
            if similarity > best_score:
                best_score = similarity
                best_role = role

        if best_score > 0:
            context.matched.append(f"Role/title: {best_role} matches {job.title}.")
            reason = f"Best role match is '{best_role}' against job title '{job.title}'."
        else:
            context.missing.append(f"Role/title: {job.title} does not match configured target roles.")
            reason = f"No configured target role overlaps the job title '{job.title}'."
        evidence = [job.title]
        if job.job_family:
            evidence.append(f"Parsed job family: {job.job_family}")
        return self._component("role_title", maximum * best_score, reason, evidence)

    def _score_skills(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["skills"]
        candidate = set(normalize_skills(profile.all_skills, self.aliases))
        required = set(normalize_skills(job.skills_required, self.aliases))
        preferred = set(normalize_skills(job.preferred_skills, self.aliases))
        if not required and not preferred:
            context.unknown.append("Skill fit: no explicit skills were extracted from the job description.")
            return self._component("skills", None, "The job description has no deterministically extracted skill requirements.", [])
        if not candidate:
            context.unknown.append("Skill fit: candidate skills are not supplied.")
            return self._component("skills", None, "Candidate skills are unknown.", [])

        matched_required = sorted(required & candidate)
        missing_required = sorted(required - candidate)
        matched_preferred = sorted(preferred & candidate)
        missing_preferred = sorted(preferred - candidate)
        for skill in matched_required:
            context.matched.append(f"Required skill: {skill}.")
        for skill in matched_preferred:
            context.matched.append(f"Preferred skill: {skill}.")
        for skill in missing_required:
            context.missing.append(f"Required skill: {skill}.")
        for skill in missing_preferred:
            context.missing.append(f"Preferred skill: {skill}.")

        required_ratio = len(matched_required) / len(required) if required else None
        preferred_ratio = len(matched_preferred) / len(preferred) if preferred else None
        if required_ratio is not None and preferred_ratio is not None:
            ratio = 0.85 * required_ratio + 0.15 * preferred_ratio
        elif required_ratio is not None:
            ratio = required_ratio
        else:
            ratio = preferred_ratio or 0.0
        reason = (
            f"Matched {len(matched_required)}/{len(required)} required skills"
            + (f" and {len(matched_preferred)}/{len(preferred)} preferred skills." if preferred else ".")
        )
        evidence = [f"matched: {', '.join(matched_required + matched_preferred) or 'none'}"]
        if missing_required or missing_preferred:
            evidence.append(f"not present in profile: {', '.join(missing_required + missing_preferred)}")
        return self._component("skills", maximum * ratio, reason, evidence)

    def _score_education(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["education"]
        if not job.education_required:
            context.unknown.append("Education fit: the job description does not state a recognized education level.")
            return self._component("education", None, "No explicit education requirement was extracted.", [])
        if not profile.education:
            context.unknown.append("Education fit: candidate education is not supplied.")
            return self._component("education", None, "Candidate education is unknown.", job.education_required)

        required_ranks = [_EDUCATION_RANKS[item] for item in job.education_required if item in _EDUCATION_RANKS]
        candidate_terms = " ".join(
            f"{item.level} {item.degree}" for item in profile.education
        ).casefold()
        candidate_ranks = [rank for term, rank in _EDUCATION_RANKS.items() if term in candidate_terms]
        if not required_ranks:
            context.unknown.append("Education fit: the extracted requirement cannot be ranked safely.")
            return self._component("education", None, "Education requirement was extracted but cannot be ranked.", job.education_required)
        if not candidate_ranks:
            context.unknown.append("Education fit: candidate degree level cannot be determined from structured fields.")
            return self._component("education", None, "Candidate degree level is unknown.", job.education_required)

        required_rank = min(required_ranks)
        candidate_rank = max(candidate_ranks)
        if candidate_rank >= required_rank:
            context.matched.append(f"Education: candidate level satisfies {', '.join(job.education_required)}.")
            return self._component("education", maximum, "Candidate education meets the explicit minimum level.", job.education_required)
        context.missing.append(f"Education: job requires {', '.join(job.education_required)}.")
        return self._component("education", 0, "Candidate education is below the explicit minimum level.", job.education_required)

    def _score_experience(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["experience"]
        requirement = job.experience_required
        if requirement is None or requirement.minimum_years is None:
            context.unknown.append("Experience fit: no explicit minimum years were extracted.")
            return self._component("experience", None, "The job's experience requirement is unknown.", [])
        if profile.years_of_experience is None:
            context.unknown.append("Experience fit: candidate years of experience are not supplied.")
            return self._component("experience", None, "Candidate years of experience are unknown.", [requirement.raw_text])

        required = requirement.minimum_years
        actual = profile.years_of_experience
        ratio = 1.0 if required <= 0 else min(actual / required, 1.0)
        if actual >= required:
            context.matched.append(f"Experience: {actual:g} years meets the {required:g}-year minimum.")
            reason = f"Candidate reports {actual:g} years against a {required:g}-year minimum."
        else:
            context.missing.append(f"Experience: {actual:g} years is below the {required:g}-year minimum.")
            reason = f"Candidate reports {actual:g} years, below the explicit {required:g}-year minimum."
        return self._component("experience", maximum * ratio, reason, [requirement.raw_text])

    def _score_industry(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["industry"]
        if not job.industries:
            context.unknown.append("Industry fit: no industry was deterministically identified for the job.")
            return self._component("industry", None, "Job industry is unknown.", [])
        if not profile.industries:
            context.unknown.append("Industry fit: candidate industry preferences are not supplied.")
            return self._component("industry", None, "Candidate industry preferences are unknown.", job.industries)
        candidate = {normalize_text_key(item) for item in profile.industries}
        job_values = {normalize_text_key(item) for item in job.industries}
        matched = sorted(candidate & job_values)
        if matched:
            context.matched.append(f"Industry/domain: {', '.join(matched)}.")
            return self._component("industry", maximum, "At least one parsed job industry matches candidate preferences.", matched)
        context.missing.append(f"Industry/domain: parsed as {', '.join(sorted(job_values))}, outside configured preferences.")
        return self._component("industry", 0, "Parsed job industries do not match configured preferences.", sorted(job_values))

    def _score_location(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["location"]
        if not job.location and not job.remote_policy:
            context.unknown.append("Location fit: job location and remote policy are not supplied.")
            return self._component("location", None, "Job location is unknown.", [])
        preferences = profile.raw.get("preferences", {})
        location_policy = (
            preferences.get("location_policy", {})
            if isinstance(preferences, dict)
            else {}
        )
        configured_local = (
            location_policy.get("local_commutable_areas", [])
            if isinstance(location_policy, dict)
            else []
        )
        local_preferences = (
            configured_local
            if isinstance(configured_local, list) and configured_local
            else profile.preferred_locations
        )
        if not local_preferences and not profile.remote_preferences:
            context.unknown.append("Location fit: candidate location preferences are not supplied.")
            return self._component(
                "location",
                None,
                "Candidate location preferences are unknown.",
                [job.location, job.remote_policy],
            )

        location_keys = _location_keys(job.location)
        preferred_location_keys = [_location_keys(str(item)) for item in local_preferences]
        location_match = any(
            any(
                candidate_key != "remote"
                and (
                    candidate_key in location_key
                    or location_key in candidate_key
                )
                for candidate_key in candidate_keys
                for location_key in location_keys
            )
            for candidate_keys in preferred_location_keys
        )
        remote_preferences = {normalize_text_key(item) for item in profile.remote_preferences}
        remote_match = bool(job.remote_policy and normalize_text_key(job.remote_policy) in remote_preferences)
        if location_match or remote_match:
            context.matched.append(f"Location: {job.location or job.remote_policy} matches configured preferences.")
            return self._component("location", maximum, "Location or work-mode preference matches.", [job.location, job.remote_policy])
        context.missing.append(f"Location: {job.location or job.remote_policy} is outside configured preferences.")
        return self._component("location", 0, "Location and work mode do not match configured preferences.", [job.location, job.remote_policy])

    def _score_salary(self, profile: CandidateProfile, job: NormalizedJob, context: _ScoreContext) -> ScoreComponent:
        maximum = self.config.scoring_weights["salary"]
        if profile.minimum_salary is None:
            context.unknown.append("Salary fit: candidate minimum salary is not supplied.")
            return self._component("salary", None, "Candidate salary minimum is unknown.", [])
        if job.salary is None or (job.salary.minimum is None and job.salary.maximum is None):
            context.unknown.append("Salary fit: job salary is not stated.")
            return self._component("salary", None, "Job salary is unknown.", [])
        if job.salary.currency != profile.salary_currency or job.salary.period != profile.salary_period:
            context.unknown.append("Salary fit: salary currency or period differs and is not converted automatically.")
            return self._component("salary", None, "Salary units are not directly comparable.", [job.salary.raw_text])

        minimum = job.salary.minimum
        maximum_value = job.salary.maximum if job.salary.maximum is not None else minimum
        if minimum is not None and minimum >= profile.minimum_salary:
            context.matched.append(f"Salary: stated minimum {minimum:g} meets candidate minimum {profile.minimum_salary:g}.")
            return self._component("salary", maximum, "Stated salary minimum meets the candidate minimum.", [job.salary.raw_text])
        if maximum_value is not None and maximum_value >= profile.minimum_salary:
            context.matched.append("Salary: stated range overlaps the candidate minimum.")
            return self._component("salary", maximum * 0.5, "Only the upper part of the stated range meets the candidate minimum.", [job.salary.raw_text])
        context.missing.append(f"Salary: stated range is below candidate minimum {profile.minimum_salary:g}.")
        return self._component("salary", 0, "Stated salary is below the candidate minimum.", [job.salary.raw_text])

    def _hard_requirements(self, profile: CandidateProfile, job: NormalizedJob) -> tuple[list[str], list[str], list[str]]:
        blockers: list[str] = []
        unknown: list[str] = []
        matched: list[str] = []
        combined = "\n".join([job.title, job.location, job.employment_type, job.job_description])

        for excluded in profile.excluded_roles:
            if _contains_phrase(job.title, excluded):
                blockers.append(f"Excluded role: job title '{job.title}' matches configured exclusion '{excluded}'.")
        for excluded in profile.excluded_locations:
            if _contains_phrase(job.location, excluded):
                blockers.append(f"Excluded location: '{job.location}' matches configured exclusion '{excluded}'.")
        for excluded in profile.excluded_conditions:
            if _contains_phrase(combined, excluded):
                blockers.append(f"Excluded condition: the job explicitly contains '{excluded}'.")

        allowed_types = profile.required_constraints.get("employment_types")
        if isinstance(allowed_types, list) and allowed_types:
            allowed = {normalize_text_key(str(item)) for item in allowed_types}
            if not job.employment_type:
                unknown.append("Hard requirement: employment type is unknown.")
            elif normalize_text_key(job.employment_type) not in allowed:
                blockers.append(
                    f"Employment type '{job.employment_type}' is outside required types: {', '.join(sorted(allowed))}."
                )
            else:
                matched.append(f"Employment type: {job.employment_type} is allowed.")

        auth = profile.work_authorization
        for requirement in job.work_authorization_requirements:
            lowered = requirement.casefold()
            no_sponsorship = any(
                phrase in lowered
                for phrase in ("without sponsorship", "no sponsorship", "unable to sponsor", "not provide sponsorship")
            )
            if no_sponsorship:
                values = (auth.requires_sponsorship_now, auth.requires_sponsorship_in_future)
                if any(value is True for value in values):
                    blockers.append(f"Work authorization conflict: job states '{requirement}'.")
                elif all(value is False for value in values):
                    matched.append("Work authorization: candidate explicitly does not require sponsorship now or later.")
                else:
                    unknown.append(f"Hard requirement unknown: sponsorship facts are incomplete for '{requirement}'.")
            elif "citizen" in lowered:
                if not auth.citizenships_complete:
                    unknown.append(f"Hard requirement unknown: citizenship facts are not declared complete for '{requirement}'.")
                elif any(normalize_text_key(item) in normalize_text_key(requirement) for item in auth.citizenships):
                    matched.append(f"Citizenship: candidate profile explicitly matches '{requirement}'.")
                else:
                    blockers.append(f"Citizenship conflict: candidate profile does not satisfy '{requirement}'.")
            elif auth.current_authorization:
                matched.append(f"Work authorization: profile contains an explicit authorization fact for '{requirement}'.")
            else:
                unknown.append(f"Hard requirement unknown: work authorization is missing for '{requirement}'.")

        candidate_licenses = {_credential_key(item) for item in auth.licenses_certifications}
        for requirement in job.licenses_required:
            key = _credential_key(requirement)
            if key in candidate_licenses:
                matched.append(f"License/certification: {requirement} is present in the candidate profile.")
            elif auth.licenses_certifications_complete:
                blockers.append(f"Required license/certification is explicitly absent: {requirement}.")
            else:
                unknown.append(f"Hard requirement unknown: candidate license/certification list is incomplete for {requirement}.")

        candidate_clearances = {_credential_key(item) for item in auth.security_clearances}
        for requirement in job.security_clearance_required:
            key = _credential_key(requirement)
            if key in candidate_clearances:
                matched.append(f"Security clearance: {requirement} is present in the candidate profile.")
            elif auth.security_clearances_complete:
                blockers.append(f"Required security clearance is explicitly absent: {requirement}.")
            else:
                unknown.append(f"Hard requirement unknown: candidate clearance list is incomplete for {requirement}.")

        return blockers, unknown, matched
