"""Deterministic first-stage job-description parsing."""

from __future__ import annotations

import re
from datetime import date
from typing import Protocol

from .config import AppConfig
from .models import ExperienceRequirement, NormalizedJob, RawJob, SalaryRange
from .normalization import (
    build_job_id,
    canonicalize_url,
    normalize_skill,
    normalize_skills,
    normalize_space,
    normalize_text_key,
)


class JobParser(Protocol):
    """Interface that deterministic or future optional parsers can implement."""

    def parse(self, raw_job: RawJob) -> NormalizedJob:
        ...


_SALARY_RE = re.compile(
    r"(?P<currency>\$|USD\s*)"
    r"(?P<minimum>\d{2,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?[kK])"
    r"(?:\s*(?:-|–|—|to)\s*"
    r"(?:\$|USD\s*)?"
    r"(?P<maximum>\d{2,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?[kK]))?"
    r"(?:\s*(?:per|/)\s*(?P<period>year|yr|annual|hour|hr|month))?",
    re.IGNORECASE,
)
_EXPERIENCE_RE = re.compile(
    r"(?P<minimum>\d+(?:\.\d+)?)\s*(?:-|–|—|to)?\s*"
    r"(?P<maximum>\d+(?:\.\d+)?)?\s*\+?\s*years?",
    re.IGNORECASE,
)
_LICENSE_RE = re.compile(
    r"(?:valid\s+)?([A-Za-z0-9][A-Za-z0-9 .+#/-]{0,45}?(?:license|certification))\s+(?:is\s+)?required",
    re.IGNORECASE,
)

_EDUCATION_PATTERNS = {
    "high school": re.compile(r"\bhigh\s+school\s+(?:diploma|degree|education|graduate)\b", re.IGNORECASE),
    "associate": re.compile(r"\bassociate(?:'s)?\s+degree\b", re.IGNORECASE),
    "bachelor": re.compile(
        r"\b(?:bachelor(?:'s)?\s+degree|bachelor\s+of\s+(?:arts|science)|"
        r"b\.?\s*[as]\.?(?:\s+degree)?|undergraduate\s+degree)\b",
        re.IGNORECASE,
    ),
    "master": re.compile(
        r"\b(?:master(?:'s)\s*(?:degree)?|master\s+(?:degree|of\s+(?:arts|science))|"
        r"m\.?\s*s\.?(?:\s+degree)?|ms\s+degree|graduate\s+degree)\b",
        re.IGNORECASE,
    ),
    "mba": re.compile(r"\b(?:mba|master\s+of\s+business\s+administration)\b", re.IGNORECASE),
    "phd": re.compile(r"\b(?:ph\.?\s*d\.?|doctor\s+of\s+philosophy)\b", re.IGNORECASE),
    "doctorate": re.compile(r"\bdoctorate(?:\s+degree)?\b", re.IGNORECASE),
}

_REQUIRED_SECTION_HEADINGS = {
    "requirements",
    "required qualifications",
    "minimum qualifications",
    "basic qualifications",
    "qualifications",
    "profile",
    "job responsibilities",
    "key responsibilities",
    "responsibilities",
    "duties",
    "what you will do",
    "what you ll do",
}
_PREFERRED_SECTION_HEADINGS = {
    "preferred",
    "preferred skills",
    "preferred qualifications",
    "desired qualifications",
    "nice to have",
    "bonus qualifications",
}
_GENERAL_SECTION_HEADINGS = {
    "company description",
    "who we are",
    "about us",
    "job description",
    "what you will learn",
    "what you will achieve",
    "physical requirements",
    "additional information",
    "what we offer you",
    "benefits",
}
_ROLE_CONTEXT_START_HEADINGS = {
    "about the role",
    "role overview",
    "position summary",
    "main job objective",
    "job description",
    *_REQUIRED_SECTION_HEADINGS,
}
_ROLE_CONTEXT_STOP_HEADINGS = {
    "additional information",
    "what we offer you",
    "benefits",
    "what you will learn",
    "what you will achieve",
    "physical requirements",
    "company description",
    "who we are",
    "about us",
}
_INDUSTRY_CONTEXT_STOP_HEADINGS = {
    "main job objective",
    "job description",
    "requirements",
    "qualifications",
    "profile",
    "job responsibilities",
    "key responsibilities",
    "responsibilities",
    "duties",
    "physical requirements",
    "additional information",
    "what we offer you",
    "benefits",
}


def _salary_number(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.replace(",", "").strip()
    multiplier = 1000 if cleaned.casefold().endswith("k") else 1
    cleaned = cleaned[:-1] if multiplier == 1000 else cleaned
    return float(cleaned) * multiplier


def _sentence_evidence(text: str, term: str) -> str:
    for line in text.splitlines():
        if term.casefold() in line.casefold():
            return normalize_space(line)
    return ""


def _heading_key(value: str) -> str:
    return normalize_text_key(value).strip()


def _matches_heading(value: str, headings: set[str]) -> bool:
    key = _heading_key(value)
    return any(key == heading or key.startswith(f"{heading} ") for heading in headings)


def _contains_marker(value: str, markers: list[str]) -> bool:
    return any(
        re.search(r"(?<!\w)" + re.escape(marker.casefold()) + r"(?!\w)", value.casefold())
        for marker in markers
        if marker
    )


def _contains_phrase(value: str, phrase: str) -> bool:
    key = normalize_text_key(value)
    phrase_key = normalize_text_key(phrase)
    return bool(
        phrase_key
        and re.search(r"(?<!\w)" + re.escape(phrase_key) + r"(?!\w)", key)
    )


def _role_context_lines(text: str) -> list[str]:
    active = False
    values: list[str] = []
    for raw_line in text.splitlines():
        line = normalize_space(raw_line)
        if not line:
            continue
        if _matches_heading(line, _ROLE_CONTEXT_STOP_HEADINGS):
            active = False
        if _matches_heading(line, _ROLE_CONTEXT_START_HEADINGS):
            active = True
            continue
        if active:
            values.append(line)
    return values


def _industry_context_lines(text: str) -> list[str]:
    values: list[str] = []
    for raw_line in text.splitlines():
        line = normalize_space(raw_line)
        if not line:
            continue
        if _matches_heading(line, _INDUSTRY_CONTEXT_STOP_HEADINGS):
            break
        values.append(line)
        if len(values) >= 20:
            break
    values.extend(
        normalize_space(line)
        for line in text.splitlines()
        if re.search(r"\b(?:industry|sector)\b", line, re.IGNORECASE)
    )
    return list(dict.fromkeys(values))


class DeterministicJobParser:
    """Extract explicit facts using regular expressions and configured terms."""

    def __init__(self, config: AppConfig):
        self.config = config
        normalization = config.normalization
        self.aliases = dict(normalization.get("skill_aliases", {}))
        configured_skills = list(normalization.get("known_skills", []))
        configured_skills.extend(self.aliases.keys())
        self.known_skills = sorted(set(configured_skills), key=len, reverse=True)

    def parse(self, raw_job: RawJob) -> NormalizedJob:
        description = (raw_job.job_description or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        evidence: dict[str, list[str]] = {}

        salary = self._parse_salary(description, evidence)
        experience = self._parse_experience(description, evidence)
        education = self._parse_education(description, evidence)
        required_skills, preferred_skills = self._parse_skills(description, evidence)
        employment_type = self._parse_employment_type(description, evidence)
        remote_policy = self._parse_remote_policy(raw_job.location, description, evidence)
        industries = self._parse_industries(description, evidence)
        job_family = self._parse_job_family(raw_job.title, description, evidence)
        licenses = self._parse_licenses(description, evidence)
        clearances = self._parse_clearances(description, evidence)
        authorization = self._parse_authorization(description, evidence)

        source_url = canonicalize_url(raw_job.source_url)
        apply_url = canonicalize_url(raw_job.apply_url)
        discovered = normalize_space(raw_job.discovered_date) or date.today().isoformat()
        job_id = build_job_id(
            source=raw_job.source,
            source_native_id=raw_job.source_native_id,
            apply_url=apply_url,
            source_url=source_url,
            company=raw_job.company,
            title=raw_job.title,
            location=raw_job.location,
        )

        return NormalizedJob(
            job_id=job_id,
            title=normalize_space(raw_job.title),
            company=normalize_space(raw_job.company),
            location=normalize_space(raw_job.location),
            source=normalize_space(raw_job.source) or "manual",
            source_url=source_url,
            apply_url=apply_url,
            job_description=description,
            salary=salary,
            employment_type=employment_type,
            experience_required=experience,
            education_required=education,
            skills_required=required_skills,
            preferred_skills=preferred_skills,
            posted_date=normalize_space(raw_job.posted_date),
            discovered_date=discovered,
            source_native_id=normalize_space(raw_job.source_native_id),
            job_family=job_family,
            industries=industries,
            remote_policy=remote_policy,
            licenses_required=licenses,
            security_clearance_required=clearances,
            work_authorization_requirements=authorization,
            parser_evidence=evidence,
        )

    def _parse_salary(self, text: str, evidence: dict[str, list[str]]) -> SalaryRange | None:
        match = _SALARY_RE.search(text)
        if not match:
            return None
        raw = normalize_space(match.group(0))
        evidence["salary"] = [raw]
        period = (match.group("period") or "year").casefold()
        period = "hour" if period in {"hour", "hr"} else "month" if period == "month" else "year"
        return SalaryRange(
            minimum=_salary_number(match.group("minimum")),
            maximum=_salary_number(match.group("maximum")),
            currency="USD",
            period=period,
            raw_text=raw,
        )

    def _parse_experience(self, text: str, evidence: dict[str, list[str]]) -> ExperienceRequirement | None:
        candidates: list[tuple[float, float | None, str]] = []
        for match in _EXPERIENCE_RE.finditer(text):
            minimum = float(match.group("minimum"))
            maximum = float(match.group("maximum")) if match.group("maximum") else None
            candidates.append((minimum, maximum, normalize_space(match.group(0))))
        if not candidates:
            return None
        minimum, maximum, raw = max(candidates, key=lambda item: item[0])
        evidence["experience_required"] = [raw]
        return ExperienceRequirement(minimum_years=minimum, maximum_years=maximum, raw_text=raw)

    def _parse_education(self, text: str, evidence: dict[str, list[str]]) -> list[str]:
        normalized_text = text.replace("’", "'")
        configured = {
            normalize_text_key(term) for term in self.config.parser.get("education_terms", [])
        }
        found: list[str] = []
        found_evidence: list[str] = []
        for level, pattern in _EDUCATION_PATTERNS.items():
            if level not in configured:
                continue
            match = pattern.search(normalized_text)
            if not match:
                continue
            found.append(level)
            source_line = _sentence_evidence(normalized_text, match.group(0))
            if source_line:
                found_evidence.append(source_line)
        if found_evidence:
            evidence["education_required"] = list(dict.fromkeys(found_evidence))
        return found

    def _parse_skills(self, text: str, evidence: dict[str, list[str]]) -> tuple[list[str], list[str]]:
        required: list[str] = []
        preferred: list[str] = []
        required_evidence: list[str] = []
        preferred_evidence: list[str] = []
        section = "general"
        required_markers = [item.casefold() for item in self.config.parser.get("required_markers", [])]
        preferred_markers = [item.casefold() for item in self.config.parser.get("preferred_markers", [])]

        for raw_line in text.splitlines():
            line = normalize_space(raw_line)
            lowered = line.casefold()
            if not line:
                continue

            if _matches_heading(line, _PREFERRED_SECTION_HEADINGS):
                section = "preferred"
            elif _matches_heading(line, _REQUIRED_SECTION_HEADINGS):
                section = "required"
            elif _matches_heading(line, _GENERAL_SECTION_HEADINGS):
                section = "general"

            line_skills: list[str] = []
            for term in self.known_skills:
                if re.search(r"(?<![A-Za-z0-9])" + re.escape(term.casefold()) + r"(?![A-Za-z0-9])", lowered):
                    line_skills.append(normalize_skill(term, self.aliases))
            line_skills = list(dict.fromkeys(line_skills))
            if "advanced excel" in line_skills:
                line_skills = [skill for skill in line_skills if skill != "excel"]
            if not line_skills:
                continue

            line_is_preferred = _contains_marker(lowered, preferred_markers)
            line_is_required = _contains_marker(lowered, required_markers)
            if "preferred but not required" in lowered:
                line_is_preferred = True
                line_is_required = False

            if line_is_preferred and not line_is_required:
                preferred.extend(line_skills)
                preferred_evidence.append(line)
            elif line_is_required:
                required.extend(line_skills)
                required_evidence.append(line)
            elif section == "preferred":
                preferred.extend(line_skills)
                preferred_evidence.append(line)
            elif section == "required":
                required.extend(line_skills)
                required_evidence.append(line)

        required_values = normalize_skills(required, self.aliases)
        preferred_values = [skill for skill in normalize_skills(preferred, self.aliases) if skill not in required_values]
        if required_evidence:
            evidence["skills_required"] = list(dict.fromkeys(required_evidence))
        if preferred_evidence:
            evidence["preferred_skills"] = list(dict.fromkeys(preferred_evidence))
        return required_values, preferred_values

    def _parse_employment_type(self, text: str, evidence: dict[str, list[str]]) -> str:
        lowered = text.casefold()
        for term in self.config.parser.get("employment_types", []):
            if term.casefold() in lowered:
                evidence["employment_type"] = [_sentence_evidence(text, term) or term]
                return term.casefold()
        return ""

    def _parse_remote_policy(self, location: str, text: str, evidence: dict[str, list[str]]) -> str:
        combined = f"{location}\n{text}".casefold()
        for term in ("hybrid", "remote", "on-site", "onsite"):
            if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", combined):
                normalized = "onsite" if term in {"on-site", "onsite"} else term
                evidence["remote_policy"] = [_sentence_evidence(f"{location}\n{text}", term) or term]
                return normalized
        return ""

    def _parse_industries(self, text: str, evidence: dict[str, list[str]]) -> list[str]:
        context_lines = _industry_context_lines(text)
        found: list[str] = []
        found_evidence: list[str] = []
        for industry, keywords in self.config.parser.get("industry_keywords", {}).items():
            matching_lines = [
                line for line in context_lines if any(_contains_phrase(line, keyword) for keyword in keywords)
            ]
            if matching_lines:
                found.append(normalize_text_key(industry))
                found_evidence.extend(matching_lines)
        if found:
            evidence["industries"] = list(dict.fromkeys(found_evidence))
        return found

    def _parse_job_family(self, title: str, text: str, evidence: dict[str, list[str]]) -> str:
        title_key = normalize_text_key(title)
        role_context = "\n".join(_role_context_lines(text))
        best_family = ""
        best_score = 0
        best_hits: list[str] = []
        for family, keywords in self.config.parser.get("job_family_keywords", {}).items():
            title_hits = [keyword for keyword in keywords if _contains_phrase(title_key, keyword)]
            role_hits = [keyword for keyword in keywords if _contains_phrase(role_context, keyword)]
            score = 8 * len(title_hits) + 3 * len(role_hits)
            if score > best_score:
                best_family = normalize_text_key(family)
                best_score = score
                best_hits = [
                    *[f"title: {item}" for item in title_hits],
                    *[f"role context: {item}" for item in role_hits],
                ]
        if best_family:
            evidence["job_family"] = list(dict.fromkeys(best_hits))
        return best_family

    def _parse_licenses(self, text: str, evidence: dict[str, list[str]]) -> list[str]:
        values = [normalize_space(match.group(1)) for match in _LICENSE_RE.finditer(text)]
        values = list(dict.fromkeys(values))
        if values:
            evidence["licenses_required"] = values.copy()
        return values

    def _parse_clearances(self, text: str, evidence: dict[str, list[str]]) -> list[str]:
        patterns = ["top secret clearance", "secret clearance", "security clearance"]
        values = [pattern for pattern in patterns if pattern in text.casefold()]
        if values:
            evidence["security_clearance_required"] = [
                _sentence_evidence(text, value) or value for value in values
            ]
        return values

    def _parse_authorization(self, text: str, evidence: dict[str, list[str]]) -> list[str]:
        markers = ("work authorization", "authorized to work", "sponsorship", "u.s. citizen", "us citizen")
        values: list[str] = []
        for line in text.splitlines():
            normalized = normalize_space(line)
            lowered = normalized.casefold()
            if any(marker in lowered for marker in markers):
                values.append(normalized)
        values = list(dict.fromkeys(values))
        if values:
            evidence["work_authorization_requirements"] = values.copy()
        return values
