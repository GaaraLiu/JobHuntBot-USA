"""Explicit approval creation, binding, and deterministic expiry."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..application_forms.models import ApplicationForm
from .fingerprints import normalized_origin, package_fingerprint
from .models import ApprovalScope, ReviewPackage, SubmissionApproval


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_submission_approval(path: str | Path) -> SubmissionApproval:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Submission approval root must be an object.")
    return SubmissionApproval.from_dict(value)


class ApprovalService:
    def create(
        self,
        review: ReviewPackage,
        package: Mapping[str, Any],
        form: ApplicationForm,
        *,
        for_submission: bool,
        confirmed_reviewed: bool,
        resolved_manual_item_ids: set[str] | None = None,
    ) -> SubmissionApproval:
        if not confirmed_reviewed:
            raise ValueError("Explicit --confirm-reviewed acknowledgment is required.")
        if review.application_id != package.get("application_id"):
            raise ValueError("ReviewPackage and ApplicationPackage application_id values differ.")
        if review.form_fingerprint != form.fingerprint:
            raise ValueError("ReviewPackage is stale for the current ApplicationForm.")
        if review.package_fingerprint != package_fingerprint(package):
            raise ValueError("ReviewPackage is stale for the current ApplicationPackage.")
        resolved_manual_item_ids = resolved_manual_item_ids or set()
        legal_items = [item.item_id for item in review.manual_items if item.category == "legal"]
        if for_submission:
            if review.blockers or review.unresolved_required_count:
                raise ValueError("Submission approval cannot be granted while required blockers remain.")
            required_manual = {
                item.item_id for item in review.manual_items
                if item.required and item.category in {"legal", "question", "unresolved_question"}
            }
            missing_manual = required_manual - resolved_manual_item_ids
            if missing_manual:
                raise ValueError(
                    "Required manual controls require individual user resolution: "
                    + ", ".join(sorted(missing_manual))
                )
        resume = next((item for item in review.documents if item.document_type == "resume"), None)
        if resume is None:
            selected_resume: dict[str, Any] = {}
        else:
            selected_resume = {
                "resume_id": resume.document_id,
                "path": resume.path,
                "sha256": resume.sha256,
                "size_bytes": resume.size_bytes,
                "status": resume.status,
            }
        cover = next((item for item in review.documents if item.document_type == "cover_letter"), None)
        origin = normalized_origin(form.application_url)
        return SubmissionApproval(
            application_id=review.application_id,
            form_fingerprint=review.form_fingerprint,
            package_fingerprint=review.package_fingerprint,
            package_version=str(package.get("updated_at", package.get("schema_version", ""))),
            selected_resume=selected_resume,
            approved_documents=[
                {
                    "document_type": item.document_type,
                    "document_id": item.document_id,
                    "path": item.path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in review.documents
                if item.status == "READY" and item.approved_in_package
            ],
            unresolved_required_count=review.unresolved_required_count,
            manual_only_items=[item.item_id for item in review.manual_items],
            salary_answer_state=review.salary_state,
            work_authorization_state=review.work_authorization_state,
            sponsorship_state=review.sponsorship_state,
            legal_consent_fields=legal_items,
            approval_status="APPROVED_FOR_SUBMISSION" if for_submission else "APPROVED_FOR_FILL",
            approved_at=utc_now(),
            approval_scope=ApprovalScope(
                allow_fill=True,
                allow_submission=for_submission,
                allow_resume_upload=bool(
                    for_submission and resume and resume.status == "READY"
                    and resume.approved_in_package
                ),
                allow_cover_letter_upload=bool(for_submission and cover and cover.status == "READY" and cover.approved_in_package),
                approved_origin=origin,
                resolved_manual_item_ids=sorted(resolved_manual_item_ids),
            ),
            review_generated_at=review.generated_at,
        )

    def validate(
        self,
        approval: SubmissionApproval,
        review: ReviewPackage,
        package: Mapping[str, Any],
        form: ApplicationForm,
    ) -> SubmissionApproval:
        reasons: list[str] = []
        if approval.application_id != str(package.get("application_id", "")):
            reasons.append("application_id changed")
        if approval.form_fingerprint != form.fingerprint or review.form_fingerprint != form.fingerprint:
            reasons.append("form fingerprint changed")
        current_package = package_fingerprint(package)
        if approval.package_fingerprint != current_package or review.package_fingerprint != current_package:
            reasons.append("application package changed")
        if approval.package_version != str(package.get("updated_at", package.get("schema_version", ""))):
            reasons.append("application package version changed")
        resume = next((item for item in review.documents if item.document_type == "resume"), None)
        if resume and (
            approval.selected_resume.get("resume_id") != resume.document_id
            or approval.selected_resume.get("sha256") != resume.sha256
            or approval.selected_resume.get("path") != resume.path
        ):
            reasons.append("selected resume changed")
        reviewed_documents = {
            (item.document_type, item.document_id): item for item in review.documents
        }
        for approved_document in approval.approved_documents:
            key = (
                str(approved_document.get("document_type", "")),
                str(approved_document.get("document_id", "")),
            )
            current = reviewed_documents.get(key)
            if current is None or any(
                approved_document.get(field) != getattr(current, field)
                for field in ("path", "sha256", "size_bytes")
            ):
                reasons.append(f"approved {key[0] or 'document'} changed")
        if approval.approval_scope.approved_origin != normalized_origin(form.application_url):
            reasons.append("application origin changed")
        if reasons:
            return replace(
                approval,
                approval_status="EXPIRED",
                invalidation_reasons=list(dict.fromkeys(reasons)),
            )
        return approval
