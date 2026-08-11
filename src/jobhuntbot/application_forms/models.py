"""Canonical read-only application-form and mapping-plan models."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


FIELD_TYPES = {
    "text", "textarea", "email", "phone", "number", "boolean", "select",
    "multiselect", "radio", "checkbox", "date", "file", "address", "resume",
    "cover_letter", "consent", "signature", "unknown",
}
MAPPING_CONFIDENCE = {"HIGH", "MEDIUM", "LOW", "UNMAPPED"}
PLAN_ACTIONS = {
    "AUTO_READY_FUTURE", "USER_CONFIRMATION", "MANUAL_ONLY", "UNRESOLVED",
    "OPTIONAL_SKIP",
}
FORM_READINESS = {"READY", "NEEDS_USER_INPUT", "BLOCKED", "INVALID"}


@dataclass(slots=True)
class ApplicationField:
    field_id: str
    section: str
    label: str
    normalized_label: str = ""
    field_type: str = "unknown"
    required: bool = False
    options: list[str] = field(default_factory=list)
    placeholder: str = ""
    help_text: str = ""
    validation_rules: dict[str, Any] = field(default_factory=dict)
    canonical_question_id: str = ""
    mapping_confidence: str = "UNMAPPED"
    safety_class: str = "UNKNOWN"
    answer_status: str = "unmapped"
    source_path: str = ""
    repeat_group: str = ""
    repeat_index: int | None = None
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FormSection:
    section_id: str
    label: str
    fields: list[ApplicationField] = field(default_factory=list)
    repeatable: bool = False
    repeat_group: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "label": self.label,
            "repeatable": self.repeatable,
            "repeat_group": self.repeat_group,
            "metadata": self.metadata,
            "fields": [item.to_dict() for item in self.fields],
        }


@dataclass(slots=True)
class ApplicationForm:
    source: str
    ats: str
    application_url: str
    job_id: str
    company: str
    title: str
    detected_at: str
    sections: list[FormSection] = field(default_factory=list)
    form_version: str = ""
    fingerprint: str = ""
    detection_status: str = "detected"
    blockers: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def fields(self) -> list[ApplicationField]:
        return [item for section in self.sections for item in section.fields]

    def compute_fingerprint(self) -> str:
        """Fingerprint structure only; candidate answers are never inputs."""

        structure = {
            "ats": self.ats,
            "sections": [
                {
                    "section_id": section.section_id,
                    "repeatable": section.repeatable,
                    "repeat_group": section.repeat_group,
                    "fields": [
                        {
                            "field_id": item.field_id,
                            "normalized_label": item.normalized_label,
                            "field_type": item.field_type,
                            "required": item.required,
                            "options": item.options,
                            "repeat_group": item.repeat_group,
                            "repeat_index": item.repeat_index,
                        }
                        for item in section.fields
                    ],
                }
                for section in self.sections
            ],
        }
        encoded = json.dumps(structure, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ats": self.ats,
            "application_url": self.application_url,
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "detected_at": self.detected_at,
            "sections": [item.to_dict() for item in self.sections],
            "form_version": self.form_version,
            "fingerprint": self.fingerprint,
            "detection_status": self.detection_status,
            "blockers": self.blockers,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ApplicationForm":
        sections = []
        for raw_section in value.get("sections", []):
            fields = [ApplicationField(**item) for item in raw_section.get("fields", [])]
            sections.append(
                FormSection(
                    section_id=str(raw_section.get("section_id", "")),
                    label=str(raw_section.get("label", "")),
                    fields=fields,
                    repeatable=bool(raw_section.get("repeatable", False)),
                    repeat_group=str(raw_section.get("repeat_group", "")),
                    metadata=dict(raw_section.get("metadata", {})),
                )
            )
        return cls(
            source=str(value.get("source", "")),
            ats=str(value.get("ats", "unknown")),
            application_url=str(value.get("application_url", "")),
            job_id=str(value.get("job_id", "")),
            company=str(value.get("company", "")),
            title=str(value.get("title", "")),
            detected_at=str(value.get("detected_at", "")),
            sections=sections,
            form_version=str(value.get("form_version", "")),
            fingerprint=str(value.get("fingerprint", "")),
            detection_status=str(value.get("detection_status", "detected")),
            blockers=[str(item) for item in value.get("blockers", [])],
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(slots=True)
class FieldMappingPlan:
    field_id: str
    label: str
    required: bool
    canonical_question_id: str
    mapping_confidence: str
    safety_class: str
    answer_status: str
    action: str
    reason: str
    source_reference: str = ""
    repeat_group: str = ""
    repeat_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ApplicationMappingPlan:
    application_id: str
    job_id: str
    form_fingerprint: str
    ats: str
    generated_at: str
    fields: list[FieldMappingPlan]
    total_fields: int
    mapped_fields: int
    required_fields: int
    required_unresolved: int
    manual_only: int
    potential_future_autofill: int
    user_confirmation: int
    optional_skip: int
    package_readiness: str
    blockers: list[str] = field(default_factory=list)
    safety_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["fields"] = [item.to_dict() for item in self.fields]
        return value
