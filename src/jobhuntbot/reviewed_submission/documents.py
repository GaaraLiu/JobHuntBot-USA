"""Exact-file validation for user-approved Phase 3.4 uploads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .fingerprints import file_sha256
from .models import ReviewDocument


def validate_pdf_document(
    *,
    document_type: str,
    document_id: str,
    configured_path: str,
    required: bool,
    approved_in_package: bool,
    constraints: Mapping[str, Any] | None = None,
) -> ReviewDocument:
    constraints = constraints or {}
    path = Path(str(configured_path or "")) if configured_path else None
    if path is None:
        return ReviewDocument(
            document_type, "MISSING_REQUIRED" if required else "NOT_PROVIDED",
            document_id, "", "", 0, required, approved_in_package,
            "No approved document path is available.",
        )
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
            raise ValueError("Document is not a PDF file.")
        with resolved.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise ValueError("Document does not have a PDF signature.")
        size = resolved.stat().st_size
    except (OSError, ValueError) as exc:
        return ReviewDocument(
            document_type, "FILE_REVIEW_REQUIRED", document_id, str(path), "", 0,
            required, approved_in_package, str(exc),
        )
    accept = str(constraints.get("accept", "")).casefold()
    if accept and ".pdf" not in accept and "application/pdf" not in accept:
        return ReviewDocument(
            document_type, "FILE_REVIEW_REQUIRED", document_id, str(resolved), "", size,
            required, approved_in_package, "Employer file-type restriction does not explicitly allow PDF.",
        )
    raw_limit = constraints.get("max_size_bytes")
    try:
        limit = int(raw_limit) if raw_limit not in {None, ""} else 0
    except (TypeError, ValueError):
        limit = 0
    if limit and size > limit:
        return ReviewDocument(
            document_type, "FILE_REVIEW_REQUIRED", document_id, str(resolved), "", size,
            required, approved_in_package, "Document exceeds the employer's declared size limit.",
        )
    return ReviewDocument(
        document_type, "READY", document_id, str(resolved), file_sha256(resolved), size,
        required, approved_in_package, "Exact readable PDF validated.",
    )
