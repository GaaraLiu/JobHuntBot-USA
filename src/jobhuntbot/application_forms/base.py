"""Common read-only application-form adapter interface and safe HTML parser."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence

from .models import ApplicationField, ApplicationForm, FIELD_TYPES, FormSection


def detected_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_REQUIRED_MARKER_SUFFIX = re.compile(
    r"(?:"
    r"\s*[\*\u204e\u2217\u2731\u2733\uff0a]+\s*"
    r"|\s*[\(\[]\s*required(?:\s+field)?\s*[\)\]]\s*"
    r"|\s+[-\u2013\u2014:]\s*required(?:\s+field)?\s*"
    r")+$",
    re.IGNORECASE,
)


def normalize_label(value: str) -> str:
    """Normalize labels while removing only unambiguous UI-required suffixes."""

    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    return _REQUIRED_MARKER_SUFFIX.sub("", text).strip()


@dataclass(slots=True)
class FormContext:
    source: str
    ats: str
    application_url: str = ""
    job_id: str = ""
    company: str = ""
    title: str = ""
    form_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ApplicationFormAdapter(ABC):
    ats: str

    @abstractmethod
    def parse_html(self, html: str, context: FormContext) -> ApplicationForm:
        """Parse already-retrieved public/user-supplied HTML without interaction."""

    @abstractmethod
    def parse_json(self, payload: Mapping[str, Any], context: FormContext) -> ApplicationForm:
        """Parse already-retrieved public/user-supplied structured metadata."""


def normalize_field_type(raw_type: str, label: str = "", *, multiple: bool = False) -> str:
    raw = str(raw_type or "").casefold().strip()
    text = normalize_label(label)
    aliases = {
        "string": "text", "short_text": "text", "long_text": "textarea",
        "integer": "number", "decimal": "number", "numeric": "number",
        "tel": "phone", "telephone": "phone", "dropdown": "select",
        "single_select": "select", "multi_select": "multiselect",
        "multi-select": "multiselect", "single_choice": "radio",
        "yes_no": "boolean", "attachment": "file", "upload": "file",
    }
    value = aliases.get(raw, raw or "unknown")
    if value == "select" and multiple:
        value = "multiselect"
    if value == "file":
        if re.search(r"\b(resume|cv)\b", text):
            return "resume"
        if "cover letter" in text:
            return "cover_letter"
    if value in {"text", "textarea", "checkbox", "boolean", "unknown"}:
        if re.search(r"\b(electronic )?signature\b", text):
            return "signature"
        if re.search(r"\b(consent|attest|certif(y|ication)|acknowledg|arbitration|privacy)\b", text):
            return "consent"
    return value if value in FIELD_TYPES else "unknown"


class _SafeFormHtmlParser(HTMLParser):
    """Extract controls only; scripts, events, and network activity are ignored."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {}
        self.sections: dict[str, dict[str, Any]] = {
            "general": {"label": "General", "repeatable": False, "repeat_group": ""}
        }
        self.section_stack = ["general"]
        self._fieldset_counter = 0
        self._label_for = ""
        self._label_parts: list[str] = []
        self._label_token = ""
        self._legend_section = ""
        self._legend_parts: list[str] = []
        self._select_index: int | None = None
        self._option_parts: list[str] = []
        self._option_value = ""
        self._textarea_index: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): "" if value is None else value for key, value in attrs}
        if tag == "fieldset":
            self._fieldset_counter += 1
            section_id = values.get("id") or values.get("data-section") or f"section_{self._fieldset_counter}"
            repeat_group = values.get("data-repeat-group", "")
            self.sections[section_id] = {
                "label": values.get("aria-label") or section_id.replace("_", " ").title(),
                "repeatable": bool(repeat_group or "data-repeatable" in values),
                "repeat_group": repeat_group,
            }
            self.section_stack.append(section_id)
        elif tag == "legend":
            self._legend_section = self.section_stack[-1]
            self._legend_parts = []
        elif tag == "label":
            self._label_for = values.get("for", "")
            self._label_token = f"nested_label_{len(self.labels)}"
            self._label_parts = []
        elif tag == "input":
            raw_type = values.get("type", "text").casefold()
            if raw_type in {"hidden", "submit", "button", "reset", "image"}:
                return
            self._add_control(values, raw_type, nested_label=self._label_token if self._label_parts is not None else "")
        elif tag == "textarea":
            self._textarea_index = self._add_control(values, "textarea", nested_label=self._label_token)
        elif tag == "select":
            raw_type = "multiselect" if "multiple" in values else "select"
            self._select_index = self._add_control(values, raw_type, nested_label=self._label_token)
        elif tag == "option" and self._select_index is not None:
            self._option_parts = []
            self._option_value = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "fieldset" and len(self.section_stack) > 1:
            self.section_stack.pop()
        elif tag == "legend" and self._legend_section:
            label = " ".join("".join(self._legend_parts).split())
            if label:
                self.sections[self._legend_section]["label"] = label
            self._legend_section = ""
            self._legend_parts = []
        elif tag == "label":
            label = " ".join("".join(self._label_parts).split())
            key = self._label_for or self._label_token
            if key and label:
                self.labels[key] = label
            self._label_for = ""
            self._label_token = ""
            self._label_parts = []
        elif tag == "option" and self._select_index is not None:
            label = " ".join("".join(self._option_parts).split()) or self._option_value
            if label:
                self.controls[self._select_index]["options"].append(label)
            self._option_parts = []
            self._option_value = ""
        elif tag == "select":
            self._select_index = None
        elif tag == "textarea":
            self._textarea_index = None

    def handle_data(self, data: str) -> None:
        if self._legend_section:
            self._legend_parts.append(data)
        if self._label_for or self._label_token:
            self._label_parts.append(data)
        if self._select_index is not None and self._option_parts is not None:
            self._option_parts.append(data)

    def _add_control(self, values: dict[str, str], raw_type: str, nested_label: str) -> int:
        field_id = values.get("id") or values.get("name") or f"field_{len(self.controls) + 1}"
        validation = {
            key: values[key]
            for key in ("min", "max", "minlength", "maxlength", "pattern", "accept")
            if key in values
        }
        control = {
            "field_id": field_id,
            "name": values.get("name", ""),
            "section_id": self.section_stack[-1],
            "raw_type": raw_type,
            "required": "required" in values or values.get("aria-required", "").casefold() == "true",
            "placeholder": values.get("placeholder", ""),
            "aria_label": values.get("aria-label", ""),
            "label_ref": values.get("id") or nested_label,
            "options": [],
            "value": values.get("value", ""),
            "validation_rules": validation,
            "source_path": f"#{values['id']}" if values.get("id") else f"[name='{values.get('name', field_id)}']",
            "repeat_group": values.get("data-repeat-group", "") or self.sections[self.section_stack[-1]].get("repeat_group", ""),
            "repeat_index": _optional_int(values.get("data-repeat-index")),
        }
        self.controls.append(control)
        return len(self.controls) - 1


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ""} else None
    except (TypeError, ValueError):
        return None


def parse_html_form(html: str, context: FormContext) -> ApplicationForm:
    parser = _SafeFormHtmlParser()
    parser.feed(html)
    sections: dict[str, FormSection] = {}
    radio_groups: dict[tuple[str, str], ApplicationField] = {}
    for raw in parser.controls:
        section_id = raw["section_id"]
        section_meta = parser.sections.get(section_id, parser.sections["general"])
        section = sections.setdefault(
            section_id,
            FormSection(
                section_id=section_id,
                label=str(section_meta["label"]),
                repeatable=bool(section_meta["repeatable"]),
                repeat_group=str(section_meta["repeat_group"]),
            ),
        )
        label = (
            parser.labels.get(raw["field_id"], "")
            or parser.labels.get(raw["label_ref"], "")
            or raw["aria_label"]
            or raw["placeholder"]
            or raw["name"]
            or raw["field_id"]
        )
        field_type = normalize_field_type(raw["raw_type"], label)
        if field_type == "radio":
            key = (section_id, raw["name"] or raw["field_id"])
            existing = radio_groups.get(key)
            option = label if label != raw["name"] else raw["value"]
            if existing is not None:
                if option and option not in existing.options:
                    existing.options.append(option)
                existing.required = existing.required or raw["required"]
                continue
        item = ApplicationField(
            field_id=raw["name"] or raw["field_id"],
            section=section.label,
            label=label,
            normalized_label=normalize_label(label),
            field_type=field_type,
            required=bool(raw["required"]),
            options=list(raw["options"]),
            placeholder=raw["placeholder"],
            validation_rules=dict(raw["validation_rules"]),
            source_path=raw["source_path"],
            repeat_group=raw["repeat_group"],
            repeat_index=raw["repeat_index"],
        )
        if field_type == "radio":
            option = label if label != raw["name"] else raw["value"]
            item.options = [option] if option else []
            radio_groups[(section_id, raw["name"] or raw["field_id"])] = item
        section.fields.append(item)

    has_fields = any(section.fields for section in sections.values())
    form = ApplicationForm(
        source=context.source,
        ats=context.ats,
        application_url=context.application_url,
        job_id=context.job_id,
        company=context.company,
        title=context.title,
        detected_at=detected_now(),
        sections=list(sections.values()),
        form_version=context.form_version,
        detection_status="detected" if has_fields else "no_fields_detected",
        blockers=[] if has_fields else ["No form controls were detected in the supplied HTML."],
        metadata={**context.metadata, "input_kind": "html"},
    )
    if has_fields and not _has_application_field_evidence(form.fields):
        form.detection_status = "no_application_form_evidence"
        form.blockers.append(
            "Page controls were preserved, but none provide deterministic application-form evidence."
        )
    form.compute_fingerprint()
    return form


_APPLICATION_FIELD_EVIDENCE = re.compile(
    r"\b(first name|last name|full name|email|phone|mobile|resume|cv|cover letter|"
    r"linkedin|address|city|state|province|postal|zip|country|work authori[sz]ation|"
    r"sponsorship|citizen|permanent resident|salary|compensation|experience|education|"
    r"employment|gender|race|ethnicity|veteran|disability|consent|signature|clearance|"
    r"driver'?s? licen[cs]e|relocat|start date|travel|website|portfolio)\b"
)


def _has_application_field_evidence(fields: Sequence[ApplicationField]) -> bool:
    """Avoid treating generic page/search controls as an application form."""

    return any(
        item.field_type in {"resume", "cover_letter", "consent", "signature"}
        or bool(_APPLICATION_FIELD_EVIDENCE.search(item.normalized_label))
        for item in fields
    )


def mapping_field(
    value: Mapping[str, Any],
    *,
    section_label: str,
    source_path: str,
    repeat_group: str = "",
    repeat_index: int | None = None,
) -> ApplicationField:
    label = str(value.get("label") or value.get("question") or value.get("name") or value.get("id") or "")
    options_raw = value.get("options") or value.get("values") or value.get("choices") or []
    options = [
        str(item.get("label") or item.get("value") or "") if isinstance(item, Mapping) else str(item)
        for item in options_raw
    ]
    raw_type = str(value.get("type") or value.get("fieldType") or value.get("format") or "unknown")
    return ApplicationField(
        field_id=str(value.get("id") or value.get("field_id") or value.get("name") or source_path),
        section=section_label,
        label=label,
        normalized_label=normalize_label(label),
        field_type=normalize_field_type(raw_type, label, multiple=bool(value.get("multiple", False))),
        required=bool(value.get("required", False)),
        options=[item for item in options if item],
        placeholder=str(value.get("placeholder", "")),
        help_text=str(value.get("help_text") or value.get("helpText") or value.get("description") or ""),
        validation_rules=dict(value.get("validation_rules") or value.get("validation") or {}),
        source_path=source_path,
        repeat_group=str(value.get("repeat_group") or repeat_group),
        repeat_index=_optional_int(value.get("repeat_index", repeat_index)),
        metadata={key: value[key] for key in ("ats_key", "schema") if key in value},
    )


def build_form(
    context: FormContext,
    sections: Sequence[FormSection],
    *,
    detection_status: str = "detected",
    blockers: list[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ApplicationForm:
    normalized_sections = list(sections)
    normalized_blockers = list(blockers or [])
    if detection_status == "detected" and not any(section.fields for section in normalized_sections):
        detection_status = "no_fields_detected"
        normalized_blockers.append("No application fields were detected in the supplied structure.")
    form = ApplicationForm(
        source=context.source,
        ats=context.ats,
        application_url=context.application_url,
        job_id=context.job_id,
        company=context.company,
        title=context.title,
        detected_at=detected_now(),
        sections=normalized_sections,
        form_version=context.form_version,
        detection_status=detection_status,
        blockers=normalized_blockers,
        metadata={**context.metadata, **dict(metadata or {})},
    )
    form.compute_fingerprint()
    return form
