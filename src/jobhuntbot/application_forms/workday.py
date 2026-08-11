"""Workday public/saved application-form adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .base import ApplicationFormAdapter, FormContext, build_form, mapping_field, parse_html_form
from .models import ApplicationForm, FormSection


class WorkdayFormAdapter(ApplicationFormAdapter):
    ats = "workday"

    def parse_html(self, html: str, context: FormContext) -> ApplicationForm:
        return parse_html_form(html, context)

    def parse_json(self, payload: Mapping[str, Any], context: FormContext) -> ApplicationForm:
        root = payload.get("applicationForm") if isinstance(payload.get("applicationForm"), Mapping) else payload
        raw_sections = root.get("sections", [])
        sections: list[FormSection] = []
        for section_index, raw_section in enumerate(raw_sections):
            if not isinstance(raw_section, Mapping):
                continue
            label = str(raw_section.get("title") or raw_section.get("label") or "Application")
            section_id = str(raw_section.get("id") or f"section_{section_index + 1}")
            repeat_group = str(raw_section.get("repeatGroup") or raw_section.get("collection") or "")
            section = FormSection(
                section_id,
                label,
                repeatable=bool(raw_section.get("repeatable", False) or repeat_group),
                repeat_group=repeat_group,
            )
            for field_index, raw in enumerate(raw_section.get("fields", [])):
                if isinstance(raw, Mapping):
                    normalized = dict(raw)
                    normalized.setdefault("type", raw.get("dataType") or raw.get("widget"))
                    normalized.setdefault("options", raw.get("instances", []))
                    section.fields.append(
                        mapping_field(
                            normalized,
                            section_label=label,
                            source_path=f"applicationForm.sections[{section_index}].fields[{field_index}]",
                            repeat_group=repeat_group,
                            repeat_index=raw_section.get("repeatIndex"),
                        )
                    )
            sections.append(section)
        return build_form(context, sections, metadata={"input_kind": "workday_json"})
