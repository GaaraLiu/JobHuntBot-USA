"""SmartRecruiters public/saved application-form adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .base import ApplicationFormAdapter, FormContext, build_form, mapping_field, parse_html_form
from .models import ApplicationForm, FormSection


class SmartRecruitersFormAdapter(ApplicationFormAdapter):
    ats = "smartrecruiters"

    def parse_html(self, html: str, context: FormContext) -> ApplicationForm:
        return parse_html_form(html, context)

    def parse_json(self, payload: Mapping[str, Any], context: FormContext) -> ApplicationForm:
        raw_sections = payload.get("sections") or payload.get("questionGroups") or []
        sections: list[FormSection] = []
        for section_index, raw_section in enumerate(raw_sections):
            if not isinstance(raw_section, Mapping):
                continue
            label = str(raw_section.get("label") or raw_section.get("name") or "Application")
            section_id = str(raw_section.get("id") or f"section_{section_index + 1}")
            repeat_group = str(raw_section.get("repeatGroup") or "")
            section = FormSection(
                section_id=section_id,
                label=label,
                repeatable=bool(repeat_group),
                repeat_group=repeat_group,
            )
            questions = raw_section.get("questions") or raw_section.get("fields") or []
            for field_index, raw in enumerate(questions):
                if isinstance(raw, Mapping):
                    normalized = dict(raw)
                    normalized.setdefault("type", raw.get("inputType"))
                    normalized.setdefault("options", raw.get("answers", []))
                    section.fields.append(
                        mapping_field(
                            normalized,
                            section_label=label,
                            source_path=f"sections[{section_index}].questions[{field_index}]",
                            repeat_group=repeat_group,
                        )
                    )
            sections.append(section)
        return build_form(context, sections, metadata={"input_kind": "smartrecruiters_json"})
