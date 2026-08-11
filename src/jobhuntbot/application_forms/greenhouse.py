"""Greenhouse public/saved application-form adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .base import ApplicationFormAdapter, FormContext, build_form, mapping_field, parse_html_form
from .models import ApplicationForm, FormSection


class GreenhouseFormAdapter(ApplicationFormAdapter):
    ats = "greenhouse"

    def parse_html(self, html: str, context: FormContext) -> ApplicationForm:
        return parse_html_form(html, context)

    def parse_json(self, payload: Mapping[str, Any], context: FormContext) -> ApplicationForm:
        groups: dict[str, FormSection] = {}
        for question_index, question in enumerate(payload.get("questions", [])):
            if not isinstance(question, Mapping):
                continue
            section_label = str(question.get("section") or "Application")
            section_id = section_label.casefold().replace(" ", "_")
            section = groups.setdefault(section_id, FormSection(section_id, section_label))
            raw_fields = question.get("fields") or [question]
            for field_index, raw in enumerate(raw_fields):
                if not isinstance(raw, Mapping):
                    continue
                merged: dict[str, Any] = dict(raw)
                merged.setdefault("label", question.get("label") or question.get("question"))
                merged.setdefault("required", question.get("required", False))
                section.fields.append(
                    mapping_field(
                        merged,
                        section_label=section_label,
                        source_path=f"questions[{question_index}].fields[{field_index}]",
                    )
                )
        return build_form(context, groups.values(), metadata={"input_kind": "greenhouse_json"})
