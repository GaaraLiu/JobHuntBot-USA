"""SmartRecruiters public Posting API adapter."""

from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .base import (
    DiscoveryTarget,
    JsonHttpClient,
    SourceBatch,
    SourceFailure,
    SourceJob,
    SourceRequestError,
    join_description_sections,
    location_from_parts,
    normalize_employment_type,
    normalize_posted_date,
)


class SmartRecruitersAdapter:
    source = "smartrecruiters"

    def __init__(self, client: JsonHttpClient):
        self.client = client

    def fetch(self, target: DiscoveryTarget, *, limit: int | None = None) -> SourceBatch:
        max_records = limit or target.max_results or (100 * target.max_pages)
        max_records = max(1, max_records)
        board = quote(target.board, safe="")
        headers = self._headers(target)
        batch = SourceBatch()
        processed = 0
        page_size = _page_size(target.options.get("page_size"))
        for page in range(target.max_pages):
            remaining = max_records - processed
            if remaining <= 0:
                break
            page_limit = min(remaining, page_size)
            params: dict[str, Any] = {
                "limit": page_limit,
                "offset": processed,
                "destination": "PUBLIC",
            }
            for key in ("country", "region", "city", "department"):
                if target.options.get(key):
                    params[key] = target.options[key]
            if target.options.get("server_query"):
                params["q"] = target.options["server_query"]
            list_url = (
                f"https://api.smartrecruiters.com/v1/companies/{board}/postings?"
                f"{urlencode(params)}"
            )
            try:
                payload = self.client.get_json(list_url, headers=headers)
            except SourceRequestError as exc:
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=exc.url,
                        error_type=type(exc).__name__,
                        reason=exc.reason,
                    )
                )
                break
            batch.pages_fetched += 1
            items = payload.get("content") if isinstance(payload, Mapping) else None
            if not isinstance(items, list):
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=list_url,
                        error_type="MalformedSourceResponse",
                        reason="SmartRecruiters response does not contain a content list.",
                    )
                )
                break
            selected = items[:remaining]
            processed += len(selected)
            for summary in selected:
                self._append_detail(batch, target, board, headers, summary, list_url)
            total_found = payload.get("totalFound") if isinstance(payload, Mapping) else None
            try:
                exhausted_total = total_found is not None and processed >= int(total_found)
            except (TypeError, ValueError):
                exhausted_total = False
            if len(items) < page_limit or exhausted_total:
                break
        return batch

    def _append_detail(
        self,
        batch: SourceBatch,
        target: DiscoveryTarget,
        board: str,
        headers: Mapping[str, str],
        summary: Any,
        list_url: str,
    ) -> None:
        job_id = ""
        detail_url = list_url
        try:
            if not isinstance(summary, Mapping):
                raise ValueError("Posting summary is not an object.")
            job_id = str(summary.get("id") or summary.get("uuid") or "").strip()
            if not job_id:
                raise ValueError("Posting is missing id and uuid.")
            detail_url = (
                f"https://api.smartrecruiters.com/v1/companies/{board}/postings/"
                f"{quote(job_id, safe='')}"
            )
            detail = self.client.get_json(detail_url, headers=headers)
            if not isinstance(detail, Mapping):
                raise ValueError("Posting detail is not an object.")
            batch.jobs.append(self._parse_job(target, summary, detail, detail_url))
        except (SourceRequestError, ValueError, TypeError) as exc:
            batch.errors.append(
                SourceFailure(
                    source=self.source,
                    url=getattr(exc, "url", detail_url),
                    job_id=job_id,
                    error_type=type(exc).__name__,
                    reason=getattr(exc, "reason", str(exc)),
                )
            )
    def _headers(self, target: DiscoveryTarget) -> dict[str, str]:
        env_name = str(target.options.get("api_token_env", "")).strip()
        if not env_name:
            return {}
        token = os.environ.get(env_name, "").strip()
        return {"X-SmartToken": token} if token else {}

    def _parse_job(
        self,
        target: DiscoveryTarget,
        summary: Mapping[str, Any],
        detail: Mapping[str, Any],
        detail_url: str,
    ) -> SourceJob:
        title = str(detail.get("name") or summary.get("name") or "").strip()
        if not title:
            raise ValueError("Posting is missing title.")
        job_id = str(detail.get("id") or summary.get("id") or detail.get("uuid") or "").strip()
        if not job_id:
            raise ValueError("Posting is missing native job id.")
        sections = detail.get("jobAd", {})
        sections = sections.get("sections", {}) if isinstance(sections, Mapping) else {}
        description_sections: list[tuple[str, Any]] = []
        if isinstance(sections, Mapping):
            ordered = (
                "companyDescription",
                "jobDescription",
                "qualifications",
                "additionalInformation",
            )
            for key in ordered:
                value = sections.get(key)
                if isinstance(value, Mapping):
                    description_sections.append(
                        (str(value.get("title", "")).strip(), value.get("text", ""))
                    )
            for key, value in sections.items():
                if key in ordered or not isinstance(value, Mapping):
                    continue
                description_sections.append(
                    (str(value.get("title", "")).strip(), value.get("text", ""))
                )
        description = join_description_sections(description_sections)
        if not description:
            raise ValueError("Posting detail is missing full job description.")

        company_value = detail.get("company") or summary.get("company") or {}
        company = (
            str(company_value.get("name", "")).strip()
            if isinstance(company_value, Mapping)
            else ""
        )
        company = company or target.company or target.board
        location_value = detail.get("location") or summary.get("location") or {}
        if not isinstance(location_value, Mapping):
            location_value = {}
        location = location_from_parts(
            location_value.get("city"),
            location_value.get("region"),
            location_value.get("country"),
        )
        employment = detail.get("typeOfEmployment") or summary.get("typeOfEmployment") or {}
        employment_label = (
            employment.get("label", "") if isinstance(employment, Mapping) else employment
        )
        apply_url = str(detail.get("applyUrl") or summary.get("applyUrl") or "").strip()
        source_url = apply_url or str(detail.get("ref") or summary.get("ref") or detail_url).strip()
        metadata = {
            "company_identifier": target.board,
            "department": detail.get("department") or summary.get("department"),
            "function": detail.get("function") or summary.get("function"),
            "industry": detail.get("industry") or summary.get("industry"),
            "experience_level": detail.get("experienceLevel") or summary.get("experienceLevel"),
            "location": dict(location_value),
            "custom_fields": detail.get("customField") or summary.get("customField") or [],
        }
        return SourceJob(
            source=self.source,
            source_job_id=job_id,
            company=company,
            title=title,
            description=description,
            location=location,
            work_mode="remote" if location_value.get("remote") is True else "",
            employment_type=normalize_employment_type(employment_label),
            posted_date=normalize_posted_date(
                detail.get("releasedDate") or summary.get("releasedDate")
            ),
            source_url=source_url,
            apply_url=apply_url,
            metadata=metadata,
        )


def _page_size(value: Any) -> int:
    try:
        return min(100, max(1, int(value or 100)))
    except (TypeError, ValueError):
        return 100
