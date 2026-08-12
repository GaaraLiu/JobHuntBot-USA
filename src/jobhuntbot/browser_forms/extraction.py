"""Convert browser-rendered structural snapshots into Phase 3.1 models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..application_forms import (
    ApplicationForm,
    ApplicationFormMapper,
    ApplicationMappingPlan,
    FormContext,
    create_form_adapter,
)
from ..answer_bank import AnswerBank
from ..application_profile import CandidateApplicationProfile
from .ats_detection import detect_rendered_ats
from .base import BrowserRenderedForm, BrowserSessionPolicy, ReadOnlyBrowserBackend


@dataclass(slots=True)
class BrowserExtractionOutcome:
    rendered: BrowserRenderedForm
    application_form: ApplicationForm
    mapping_plan: ApplicationMappingPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "safety_boundary": {
                "mode": "READ_ONLY",
                "candidate_data_typed": False,
                "files_uploaded": False,
                "forms_submitted": False,
                "legal_controls_accepted": False,
            },
            "browser": self.rendered.to_dict(),
            "application_form": self.application_form.to_dict(),
            "mapping_plan": self.mapping_plan.to_dict() if self.mapping_plan else None,
        }


class BrowserFormExtractionService:
    """Orchestrate a read-only backend and the existing Phase 3.1 pipeline."""

    def __init__(self, backend: ReadOnlyBrowserBackend):
        self.backend = backend

    def inspect(
        self,
        url: str,
        *,
        policy: BrowserSessionPolicy | None = None,
        known_source: str = "",
        job_id: str = "",
        company: str = "",
        title: str = "",
        profile: CandidateApplicationProfile | None = None,
        answer_bank: AnswerBank | None = None,
        application_id: str = "",
        job: Mapping[str, Any] | None = None,
        selected_resume: Mapping[str, Any] | None = None,
    ) -> BrowserExtractionOutcome:
        rendered = self.backend.inspect(url, policy or BrowserSessionPolicy())
        ats = detect_rendered_ats(
            rendered.final_url or rendered.requested_url,
            known_source=known_source or rendered.ats_hint,
            dom_markers=rendered.metadata.get("dom_markers", []),
        )
        context = FormContext(
            source="browser_rendered_read_only",
            ats=ats,
            application_url=rendered.final_url or rendered.requested_url,
            job_id=job_id,
            company=company,
            title=title or rendered.page_title,
            metadata={
                "browser_status": rendered.status,
                "current_step": rendered.current_step,
                "step_count": rendered.step_count,
                "later_steps_unavailable_without_submission": rendered.later_steps_unavailable_without_submission,
                "login_wall": rendered.login_wall,
                "anti_bot": rendered.anti_bot,
                "apply_navigation_followed": rendered.apply_navigation_followed,
                "blocked_request_count": rendered.blocked_request_count,
                "blocked_requests": rendered.metadata.get("blocked_requests", []),
                "dom_markers": rendered.metadata.get("dom_markers", []),
                "input_kind": "browser_rendered_structure",
            },
        )
        payload = self._phase31_payload(rendered)
        form = create_form_adapter("unknown").parse_json(payload, context)
        form.ats = ats
        form.metadata.update(context.metadata)
        self._attach_browser_metadata(form, rendered)
        if rendered.status not in {"DETECTED", "PARTIAL"}:
            form.detection_status = rendered.status
        elif rendered.status == "PARTIAL":
            form.detection_status = "partial"
        form.blockers = list(dict.fromkeys([*form.blockers, *rendered.blockers]))
        form.compute_fingerprint()

        mapping_plan = None
        if (profile is not None or answer_bank is not None) and rendered.status in {
            "DETECTED",
            "PARTIAL",
            "BLOCKED_BY_ANTI_BOT",
        }:
            if profile is None or answer_bank is None:
                raise ValueError("Both application profile and answer bank are required for local mapping.")
            mapping_plan = ApplicationFormMapper(profile, answer_bank).build_plan(
                form,
                application_id=application_id,
                job=job,
                selected_resume=selected_resume,
            )
        return BrowserExtractionOutcome(rendered, form, mapping_plan)

    @staticmethod
    def _phase31_payload(rendered: BrowserRenderedForm) -> dict[str, Any]:
        grouped: dict[tuple[str, str, str, int | None], list[dict[str, Any]]] = {}
        for item in rendered.fields:
            key = (item.section_id or "application", item.section_label or "Application", item.repeat_group, item.repeat_index)
            grouped.setdefault(key, []).append(
                {
                    "id": item.field_id,
                    "label": item.label or item.accessible_name,
                    "type": item.dom_type,
                    "required": item.required,
                    "options": item.options,
                    "placeholder": item.placeholder,
                    "help_text": item.help_text,
                    "validation_rules": item.validation_rules,
                    "repeat_group": item.repeat_group,
                    "repeat_index": item.repeat_index,
                }
            )
        return {
            "sections": [
                {
                    "id": section_id,
                    "label": label,
                    "repeat_group": repeat_group,
                    "repeat_index": repeat_index,
                    "repeatable": bool(repeat_group),
                    "fields": fields,
                }
                for (section_id, label, repeat_group, repeat_index), fields in grouped.items()
            ]
        }

    @staticmethod
    def _attach_browser_metadata(form: ApplicationForm, rendered: BrowserRenderedForm) -> None:
        snapshots = {item.field_id: item for item in rendered.fields}
        for field in form.fields:
            item = snapshots.get(field.field_id)
            if item is None:
                continue
            field.source_path = item.locator
            field.metadata.update(
                {
                    "browser_visible": item.visible,
                    "browser_hidden": item.hidden,
                    "browser_disabled": item.disabled,
                    "accessible_name": item.accessible_name,
                    "dom_type": item.dom_type,
                }
            )
