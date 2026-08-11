"""Lever public/saved application-form adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .base import ApplicationFormAdapter, FormContext, build_form, mapping_field, parse_html_form
from .models import ApplicationForm, FormSection


class LeverFormAdapter(ApplicationFormAdapter):
    ats = "lever"

    def parse_html(self, html: str, context: FormContext) -> ApplicationForm:
        return parse_html_form(html, context)

    def parse_json(self, payload: Mapping[str, Any], context: FormContext) -> ApplicationForm:
        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list):
            raw_sections = [{"id": "application", "label": "Application", "fields": payload.get("fields", [])}]
        sections: list[FormSection] = []
        for section_index, raw_section in enumerate(raw_sections):
            if not isinstance(raw_section, Mapping):
                continue
            label = str(raw_section.get("label") or raw_section.get("name") or "Application")
            section_id = str(raw_section.get("id") or f"section_{section_index + 1}")
            repeat_group = str(raw_section.get("repeat_group") or "")
            section = FormSection(
                section_id,
                label,
                repeatable=bool(raw_section.get("repeatable", False)),
                repeat_group=repeat_group,
            )
            for field_index, raw in enumerate(raw_section.get("fields", [])):
                if isinstance(raw, Mapping):
                    section.fields.append(
                        mapping_field(
                            raw,
                            section_label=label,
                            source_path=f"sections[{section_index}].fields[{field_index}]",
                            repeat_group=repeat_group,
                            repeat_index=raw_section.get("repeat_index"),
                        )
                    )
            sections.append(section)
        return build_form(context, sections, metadata={"input_kind": "lever_json"})
