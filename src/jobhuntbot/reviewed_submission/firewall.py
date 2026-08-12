"""Narrow origin/application-scoped transition from read-only to submission."""

from __future__ import annotations

from .fingerprints import normalized_origin
from .models import SubmissionApproval


class ScopedSubmissionFirewall:
    def __init__(self, *, allowed_mutation_paths: set[str] | None = None) -> None:
        self.state = "LOCKED"
        self.application_id = ""
        self.approved_origin = ""
        self.blocked: list[dict[str, str]] = []
        self.allowed_mutations: list[dict[str, str]] = []
        self.allowed_mutation_paths = set(allowed_mutation_paths or set())

    def arm(self, approval: SubmissionApproval) -> None:
        if approval.approval_status != "APPROVED_FOR_SUBMISSION":
            raise ValueError("Submission firewall requires APPROVED_FOR_SUBMISSION.")
        if not approval.approval_scope.allow_submission:
            raise ValueError("Approval scope does not permit submission mutations.")
        if not approval.approval_scope.approved_origin:
            raise ValueError("Approval has no valid application origin.")
        self.state = "ARMED"
        self.application_id = approval.application_id
        self.approved_origin = approval.approval_scope.approved_origin

    def begin_submission(self, application_id: str) -> None:
        if self.state != "ARMED" or application_id != self.application_id:
            raise ValueError("Submission firewall is not armed for this application.")
        self.state = "SUBMITTING"

    def close(self) -> None:
        self.state = "CLOSED"

    def allows(self, method: str, url: str, *, application_id: str) -> bool:
        method = str(method or "GET").upper()
        origin = normalized_origin(url)
        from urllib.parse import urlsplit
        path = urlsplit(str(url or "")).path
        if method in {"GET", "HEAD", "OPTIONS"}:
            return True
        entry = {"method": method, "origin": origin or "UNKNOWN"}
        permitted = (
            method in {"POST", "PUT", "PATCH"}
            and self.state in {"ARMED", "SUBMITTING"}
            and application_id == self.application_id
            and origin == self.approved_origin
            and (not self.allowed_mutation_paths or path in self.allowed_mutation_paths)
        )
        if permitted:
            self.allowed_mutations.append(entry)
            return True
        self.blocked.append(entry)
        return False
