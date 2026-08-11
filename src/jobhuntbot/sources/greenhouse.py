"""Greenhouse public Job Board API adapter."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from .base import (
    DiscoveryTarget,
    JsonHttpClient,
    SourceBatch,
    SourceFailure,
    SourceJob,
    SourceRequestError,
    html_to_text,
    normalize_employment_type,
    normalize_posted_date,
    normalize_work_mode,
)


class GreenhouseAdapter:
    source = "greenhouse"

    def __init__(self, client: JsonHttpClient):
        self.client = client

    def fetch(self, target: DiscoveryTarget, *, limit: int | None = None) -> SourceBatch:
        board = quote(target.board, safe="")
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        batch = SourceBatch()
        try:
            payload = self.client.get_json(url)
        except SourceRequestError as exc:
            batch.errors.append(
                SourceFailure(
                    source=self.source,
                    url=exc.url,
                    error_type=type(exc).__name__,
                    reason=exc.reason,
                )
            )
            return batch
        items = payload.get("jobs") if isinstance(payload, Mapping) else None
        if not isinstance(items, list):
            batch.errors.append(
                SourceFailure(
                    source=self.source,
                    url=url,
                    error_type="MalformedSourceResponse",
                    reason="Greenhouse response does not contain a jobs list.",
                )
            )
            return batch
        selected = items[:limit] if limit is not None else items
        for item in selected:
            job_id = ""
            try:
                if not isinstance(item, Mapping):
                    raise ValueError("Posting is not an object.")
                job_id = str(item.get("id") or "").strip()
                title = str(item.get("title") or "").strip()
                description = html_to_text(item.get("content"))
                if not job_id:
                    raise ValueError("Posting is missing native job id.")
                if not title:
                    raise ValueError("Posting is missing title.")
                if not description:
                    raise ValueError("Posting is missing full job description.")
                location_value = item.get("location") or {}
                location = (
                    str(location_value.get("name", "")).strip()
                    if isinstance(location_value, Mapping)
                    else str(location_value).strip()
                )
                absolute_url = str(item.get("absolute_url") or "").strip()
                metadata = {
                    "board": target.board,
                    "internal_job_id": item.get("internal_job_id"),
                    "requisition_id": item.get("requisition_id"),
                    "updated_at": item.get("updated_at"),
                    "departments": item.get("departments") or [],
                    "offices": item.get("offices") or [],
                    "language": item.get("language"),
                    "metadata": item.get("metadata"),
                }
                batch.jobs.append(
                    SourceJob(
                        source=self.source,
                        source_job_id=job_id,
                        company=target.company or target.board,
                        title=title,
                        description=description,
                        location=location,
                        work_mode=normalize_work_mode(item.get("workplace_type")),
                        employment_type=normalize_employment_type(
                            item.get("employment_type")
                        ),
                        posted_date=normalize_posted_date(item.get("first_published")),
                        source_url=absolute_url,
                        apply_url=absolute_url,
                        metadata=metadata,
                    )
                )
            except (ValueError, TypeError) as exc:
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=url,
                        job_id=job_id,
                        error_type=type(exc).__name__,
                        reason=str(exc),
                    )
                )
        return batch
