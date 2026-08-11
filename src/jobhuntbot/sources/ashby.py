"""Ashby public Job Postings API adapter."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

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


class AshbyAdapter:
    source = "ashby"

    def __init__(self, client: JsonHttpClient):
        self.client = client

    def fetch(self, target: DiscoveryTarget, *, limit: int | None = None) -> SourceBatch:
        board = quote(target.board, safe="")
        url = (
            f"https://api.ashbyhq.com/posting-api/job-board/{board}"
            "?includeCompensation=true"
        )
        batch = SourceBatch()
        try:
            payload = self.client.get_json(url)
            batch.pages_fetched = 1
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
                    reason="Ashby response does not contain a jobs list.",
                )
            )
            return batch
        visible = [
            item for item in items
            if isinstance(item, Mapping) and item.get("isListed", True) is not False
        ]
        selected = visible[:limit] if limit is not None else visible
        for item in selected:
            job_id = ""
            try:
                title = str(item.get("title") or "").strip()
                job_url = str(item.get("jobUrl") or "").strip()
                apply_url = str(item.get("applyUrl") or "").strip()
                job_id = str(item.get("id") or _url_identifier(job_url) or "").strip()
                description = str(item.get("descriptionPlain") or "").strip()
                if not description:
                    description = html_to_text(item.get("descriptionHtml"))
                if not title:
                    raise ValueError("Posting is missing title.")
                if not description:
                    raise ValueError("Posting is missing full job description.")
                if not job_id and not job_url:
                    raise ValueError("Posting is missing native id and canonical job URL.")
                batch.jobs.append(
                    SourceJob(
                        source=self.source,
                        source_job_id=job_id,
                        company=target.company or target.board,
                        title=title,
                        description=description,
                        location=str(item.get("location") or "").strip(),
                        work_mode=(normalize_work_mode(item.get("workplaceType")) or ("remote" if item.get("isRemote") is True else "")),
                        employment_type=normalize_employment_type(
                            item.get("employmentType")
                        ),
                        posted_date=normalize_posted_date(item.get("publishedAt")),
                        salary=self._salary(item.get("compensation")),
                        source_url=job_url,
                        apply_url=apply_url,
                        metadata={
                            "board": target.board,
                            "department": item.get("department"),
                            "team": item.get("team"),
                            "secondary_locations": item.get("secondaryLocations") or [],
                            "address": item.get("address"),
                            "is_remote": item.get("isRemote"),
                            "workplace_type": item.get("workplaceType"),
                            "compensation": item.get("compensation"),
                        },
                    )
                )
            except (ValueError, TypeError) as exc:
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=str(item.get("jobUrl") or url),
                        job_id=job_id,
                        error_type=type(exc).__name__,
                        reason=str(exc),
                    )
                )
        return batch

    def _salary(self, value: Any) -> SourceSalary | None:
        if not isinstance(value, Mapping):
            return None
        components = value.get("summaryComponents")
        if not isinstance(components, list):
            components = []
        salaries = [
            item for item in components
            if isinstance(item, Mapping)
            and str(item.get("compensationType") or "").casefold() == "salary"
        ]
        if not salaries:
            raw = str(value.get("scrapeableCompensationSalarySummary") or "").strip()
            return SourceSalary(raw_text=raw) if raw else None
        item = salaries[0]
        interval = str(item.get("interval") or "").casefold()
        period = "year" if "year" in interval else "month" if "month" in interval else "hour" if "hour" in interval else ""
        return SourceSalary(
            minimum=_number(item.get("minValue")),
            maximum=_number(item.get("maxValue")),
            currency=str(item.get("currencyCode") or "").strip().upper(),
            period=period,
            raw_text=str(
                value.get("scrapeableCompensationSalarySummary")
                or value.get("compensationTierSummary")
                or ""
            ).strip(),
        )


def _url_identifier(value: str) -> str:
    if not value:
        return ""
    path = PurePosixPath(urlsplit(value).path)
    return path.name


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
