"""ATS detection and application-form adapter registry."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .ashby import AshbyFormAdapter
from .base import ApplicationFormAdapter, FormContext, build_form, mapping_field, parse_html_form
from .greenhouse import GreenhouseFormAdapter
from .lever import LeverFormAdapter
from .models import ApplicationForm, FormSection
from .smartrecruiters import SmartRecruitersFormAdapter
from .workday import WorkdayFormAdapter


SUPPORTED_APPLICATION_ATS = (
    "ashby", "greenhouse", "lever", "smartrecruiters", "workday"
)


class GenericFormAdapter(ApplicationFormAdapter):
    ats = "unknown"

    def parse_html(self, html: str, context: FormContext) -> ApplicationForm:
        return parse_html_form(html, context)

    def parse_json(self, payload: Mapping[str, Any], context: FormContext) -> ApplicationForm:
        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list):
            raw_sections = [{"id": "application", "label": "Application", "fields": payload.get("fields") or payload.get("questions") or []}]
        sections: list[FormSection] = []
        for section_index, raw_section in enumerate(raw_sections):
            if not isinstance(raw_section, Mapping):
                continue
            label = str(raw_section.get("label") or raw_section.get("title") or raw_section.get("name") or "Application")
            section_id = str(raw_section.get("id") or f"section_{section_index + 1}")
            repeat_group = str(raw_section.get("repeat_group") or raw_section.get("repeatGroup") or "")
            section = FormSection(
                section_id=section_id,
                label=label,
                repeatable=bool(raw_section.get("repeatable", False) or repeat_group),
                repeat_group=repeat_group,
            )
            for field_index, raw in enumerate(raw_section.get("fields") or raw_section.get("questions") or []):
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
        return build_form(context, sections, metadata={"input_kind": "manual_json"})


_ADAPTERS: dict[str, type[ApplicationFormAdapter]] = {
    "greenhouse": GreenhouseFormAdapter,
    "lever": LeverFormAdapter,
    "ashby": AshbyFormAdapter,
    "smartrecruiters": SmartRecruitersFormAdapter,
    "workday": WorkdayFormAdapter,
}


def detect_application_ats(application_url: str = "", known_source: str = "") -> str:
    """Reuse a known Phase 2 source, otherwise use deterministic URL evidence."""

    source = str(known_source or "").casefold().strip()
    if source in SUPPORTED_APPLICATION_ATS:
        return source
    try:
        host = (urlsplit(application_url).hostname or "").casefold()
        path = urlsplit(application_url).path.casefold()
    except ValueError:
        return "unknown"
    if host.endswith("greenhouse.io") or "greenhouse" in host:
        return "greenhouse"
    if host == "jobs.lever.co" or host.endswith(".lever.co"):
        return "lever"
    if host == "jobs.ashbyhq.com" or host.endswith(".ashbyhq.com"):
        return "ashby"
    if host.endswith("smartrecruiters.com") or "smartrecruiters" in host:
        return "smartrecruiters"
    if host.endswith("myworkdayjobs.com") or re.search(r"/wday/(cxs|apply)/", path):
        return "workday"
    return "unknown"


def create_form_adapter(ats: str) -> ApplicationFormAdapter:
    normalized = str(ats or "unknown").casefold()
    adapter_type = _ADAPTERS.get(normalized, GenericFormAdapter)
    adapter = adapter_type()
    if normalized == "unknown":
        adapter.ats = "unknown"
    return adapter


def registered_application_ats() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
