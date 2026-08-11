"""Lever public Postings API adapter."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .base import (
    DiscoveryTarget,
    JsonHttpClient,
    SourceBatch,
    SourceFailure,
    SourceJob,
    SourceRequestError,
    SourceSalary,
    html_to_text,
    normalize_employment_type,
    normalize_posted_date,
    normalize_work_mode,
)


class LeverAdapter:
    source = "lever"

    def __init__(self, client: JsonHttpClient):
        self.client = client

    def fetch(self, target: DiscoveryTarget, *, limit: int | None = None) -> SourceBatch:
        base = (
            "https://api.eu.lever.co"
            if target.options.get("region") == "eu"
            else "https://api.lever.co"
        )
        max_records = limit or target.max_results or (100 * target.max_pages)
        max_records = max(1, max_records)
        batch = SourceBatch()
        processed = 0
        page_size = _page_size(target.options.get("page_size"))
        for _page in range(target.max_pages):
            remaining = max_records - processed
            if remaining <= 0:
                break
            page_limit = min(remaining, page_size)
            params: dict[str, Any] = {
                "mode": "json",
                "skip": processed,
                "limit": page_limit,
            }
            url = (
                f"{base}/v0/postings/{quote(target.board, safe='')}?"
                f"{urlencode(params)}"
            )
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
                break
            batch.pages_fetched += 1
            if not isinstance(payload, list):
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=url,
                        error_type="MalformedSourceResponse",
                        reason="Lever response is not a postings list.",
                    )
                )
                break
            selected = payload[:remaining]
            processed += len(selected)
            for item in selected:
                self._append_job(batch, target, item, url)
            if len(payload) < page_limit:
                break
        return batch

    def _append_job(
        self,
        batch: SourceBatch,
        target: DiscoveryTarget,
        item: Any,
        url: str,
    ) -> None:
        job_id = ""
        try:
            if not isinstance(item, Mapping):
                raise ValueError("Posting is not an object.")
            job_id = str(item.get("id") or "").strip()
            title = str(item.get("text") or "").strip()
            if not job_id:
                raise ValueError("Posting is missing native job id.")
            if not title:
                raise ValueError("Posting is missing title.")
            description = self._description(item)
            if not description:
                raise ValueError("Posting is missing full job description.")
            categories = item.get("categories") or {}
            if not isinstance(categories, Mapping):
                categories = {}
            hosted_url = str(item.get("hostedUrl") or "").strip()
            apply_url = str(item.get("applyUrl") or "").strip()
            batch.jobs.append(
                SourceJob(
                    source=self.source,
                    source_job_id=job_id,
                    company=target.company or target.board,
                    title=title,
                    description=description,
                    location=str(categories.get("location") or "").strip(),
                    work_mode=normalize_work_mode(item.get("workplaceType")),
                    employment_type=normalize_employment_type(
                        categories.get("commitment")
                    ),
                    posted_date=normalize_posted_date(
                        item.get("createdAt") or item.get("created_at")
                    ),
                    salary=self._salary(item.get("salaryRange")),
                    source_url=hosted_url,
                    apply_url=apply_url,
                    metadata={
                        "board": target.board,
                        "categories": dict(categories),
                        "country": item.get("country"),
                        "workplace_type": item.get("workplaceType"),
                        "salary_description": item.get("salaryDescriptionPlain"),
                    },
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
    def _description(self, item: Mapping[str, Any]) -> str:
        values: list[str] = []
        primary = str(item.get("descriptionPlain") or "").strip()
        if not primary:
            primary = html_to_text(item.get("description"))
        if primary:
            values.append(primary)
        lists = item.get("lists")
        if isinstance(lists, list):
            for value in lists:
                if not isinstance(value, Mapping):
                    continue
                heading = str(value.get("text") or "").strip()
                content = html_to_text(value.get("content"))
                if heading and content:
                    values.append(f"{heading}\n{content}")
        additional = str(item.get("additionalPlain") or "").strip()
        if not additional:
            additional = html_to_text(item.get("additional"))
        if additional:
            values.append(additional)
        return "\n\n".join(values).strip()

    def _salary(self, value: Any) -> SourceSalary | None:
        if not isinstance(value, Mapping):
            return None
        minimum = _number(value.get("min"))
        maximum = _number(value.get("max"))
        if minimum is None and maximum is None:
            return None
        interval = str(value.get("interval") or "").strip().casefold()
        period = "year" if "year" in interval else "month" if "month" in interval else "hour" if "hour" in interval else interval
        return SourceSalary(
            minimum=minimum,
            maximum=maximum,
            currency=str(value.get("currency") or "").strip().upper(),
            period=period,
            raw_text=str(value.get("description") or "").strip(),
        )


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _page_size(value: Any) -> int:
    try:
        return min(100, max(1, int(value or 100)))
    except (TypeError, ValueError):
        return 100
