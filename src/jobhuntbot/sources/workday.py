"""Workday public CXS careers-site adapter."""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlsplit

from .base import (
    DiscoveryTarget,
    JsonHttpClient,
    SourceBatch,
    SourceConfigurationError,
    SourceFailure,
    SourceJob,
    SourceRequestError,
    html_to_text,
    normalize_employment_type,
    normalize_posted_date,
    normalize_work_mode,
)


class WorkdayAdapter:
    """Read one configured public Workday CXS career site with bounded paging."""

    source = "workday"

    def __init__(self, client: JsonHttpClient):
        self.client = client

    def fetch(self, target: DiscoveryTarget, *, limit: int | None = None) -> SourceBatch:
        config = _target_config(target)
        max_records = max(1, limit or target.max_results or (20 * target.max_pages))
        page_size = _page_size(target.options.get("page_size"))
        batch = SourceBatch()
        processed = 0

        for _page in range(target.max_pages):
            remaining = max_records - processed
            if remaining <= 0:
                break
            request_limit = min(page_size, remaining)
            payload: dict[str, Any] = {
                "appliedFacets": _mapping(target.options.get("applied_facets")),
                "limit": request_limit,
                "offset": processed,
                "searchText": str(target.options.get("server_query") or "").strip(),
            }
            try:
                response = self.client.post_json(config.search_url, payload)
            except SourceRequestError as exc:
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=exc.url,
                        error_type=_request_error_type(exc),
                        reason=exc.reason,
                    )
                )
                break

            batch.pages_fetched += 1
            items = response.get("jobPostings") if isinstance(response, Mapping) else None
            if not isinstance(items, list):
                batch.errors.append(
                    SourceFailure(
                        source=self.source,
                        url=config.search_url,
                        error_type="MalformedSourceResponse",
                        reason="Workday response does not contain a jobPostings list.",
                    )
                )
                break

            selected = items[:remaining]
            processed += len(selected)
            for summary in selected:
                self._append_detail(batch, target, config, summary)

            total = response.get("total") if isinstance(response, Mapping) else None
            exhausted_total = False
            try:
                exhausted_total = total is not None and processed >= int(total)
            except (TypeError, ValueError):
                pass
            if len(items) < request_limit or exhausted_total:
                break
        return batch

    def _append_detail(
        self,
        batch: SourceBatch,
        target: DiscoveryTarget,
        config: "_WorkdayConfig",
        summary: Any,
    ) -> None:
        external_path = ""
        detail_url = config.search_url
        try:
            if not isinstance(summary, Mapping):
                raise ValueError("Workday posting summary is not an object.")
            external_path = str(summary.get("externalPath") or "").strip()
            if not external_path:
                raise ValueError("Workday posting summary is missing externalPath.")
            detail_url = config.detail_url(external_path)
            response = self.client.get_json(detail_url)
            if not isinstance(response, Mapping):
                raise ValueError("Workday posting detail is not an object.")
            detail = response.get("jobPostingInfo", response)
            if not isinstance(detail, Mapping):
                raise ValueError("Workday response does not contain jobPostingInfo.")
            batch.jobs.append(
                self._parse_job(target, config, summary, detail, external_path, detail_url)
            )
        except (SourceRequestError, ValueError, TypeError) as exc:
            batch.errors.append(
                SourceFailure(
                    source=self.source,
                    url=getattr(exc, "url", detail_url),
                    job_id=_path_identifier(external_path),
                    error_type=(
                        _request_error_type(exc)
                        if isinstance(exc, SourceRequestError)
                        else type(exc).__name__
                    ),
                    reason=getattr(exc, "reason", str(exc)),
                )
            )

    def _parse_job(
        self,
        target: DiscoveryTarget,
        config: "_WorkdayConfig",
        summary: Mapping[str, Any],
        detail: Mapping[str, Any],
        external_path: str,
        detail_url: str,
    ) -> SourceJob:
        title = str(detail.get("title") or summary.get("title") or "").strip()
        description = html_to_text(detail.get("jobDescription"))
        source_job_id = str(
            detail.get("jobReqId")
            or detail.get("jobPostingId")
            or detail.get("id")
            or _path_identifier(external_path)
        ).strip()
        if not source_job_id:
            raise ValueError("Workday posting is missing a stable native id and external path.")
        if not title:
            raise ValueError("Workday posting is missing title.")
        if not description:
            raise ValueError("Workday posting detail is missing full job description.")

        location = _text_value(detail.get("location")) or str(
            summary.get("locationsText") or ""
        ).strip()
        country = detail.get("country")
        country_code = detail.get("countryCode")
        remote_value = detail.get("remoteType") or detail.get("workplaceType")
        work_mode = normalize_work_mode(_text_value(remote_value))
        employment = detail.get("timeType") or detail.get("workerSubType")
        external_url = str(detail.get("externalUrl") or "").strip()
        public_url = external_url or config.public_job_url(external_path)

        return SourceJob(
            source=self.source,
            source_job_id=source_job_id,
            company=target.company or target.board,
            title=title,
            description=description,
            location=location,
            work_mode=work_mode,
            employment_type=normalize_employment_type(_text_value(employment)),
            posted_date=normalize_posted_date(
                detail.get("postedDate") or detail.get("postedOn") or summary.get("postedOn")
            ),
            updated_date=normalize_posted_date(detail.get("updatedDate")),
            source_url=public_url,
            apply_url=external_url or public_url,
            metadata={
                "tenant": config.tenant,
                "career_site": config.career_site,
                "external_path": external_path,
                "detail_endpoint": detail_url,
                "country": _text_value(country),
                "country_code": _text_value(country_code),
                "country_raw": country,
                "additional_locations": detail.get("additionalLocations") or [],
                "remote_type": remote_value,
                "time_type": detail.get("timeType"),
                "worker_sub_type": detail.get("workerSubType"),
                "job_profile": detail.get("jobProfile"),
                "start_date": detail.get("startDate"),
                "posted_on_raw": detail.get("postedOn") or summary.get("postedOn"),
                "bullet_fields": summary.get("bulletFields") or [],
            },
        )


def validate_workday_target(target: DiscoveryTarget) -> None:
    """Validate Workday endpoint configuration without making a network request."""

    _target_config(target)


class _WorkdayConfig:
    def __init__(
        self,
        *,
        base_url: str,
        tenant: str,
        career_site: str,
        locale: str,
        search_path: str,
        detail_path_template: str,
    ):
        self.base_url = base_url
        self.tenant = tenant
        self.career_site = career_site
        self.locale = locale
        self.search_path = search_path
        self.detail_path_template = detail_path_template
        self.search_url = urljoin(f"{base_url}/", search_path.lstrip("/"))

    def detail_url(self, external_path: str) -> str:
        value = self.detail_path_template.format(
            tenant=quote(self.tenant, safe=""),
            career_site=quote(self.career_site, safe=""),
            external_path=external_path.lstrip("/"),
        )
        return urljoin(f"{self.base_url}/", value.lstrip("/"))

    def public_job_url(self, external_path: str) -> str:
        path = f"/{quote(self.locale, safe='-')}/{quote(self.career_site, safe='')}"
        path += "/" + external_path.lstrip("/")
        return urljoin(f"{self.base_url}/", path.lstrip("/"))


def _target_config(target: DiscoveryTarget) -> _WorkdayConfig:
    base_url = str(target.options.get("base_url") or "").strip().rstrip("/")
    tenant = str(target.options.get("tenant") or "").strip()
    career_site = str(target.options.get("career_site") or "").strip()
    if not base_url or not tenant or not career_site:
        raise SourceConfigurationError(
            "Workday target requires base_url, tenant, and career_site."
        )
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise SourceConfigurationError(
            "Workday base_url must be an HTTPS origin without query or fragment."
        )
    if parsed.username or parsed.password:
        raise SourceConfigurationError("Workday base_url cannot contain credentials.")
    locale = str(target.options.get("locale") or "en-US").strip()
    search_path = str(
        target.options.get("search_path")
        or f"/wday/cxs/{quote(tenant, safe='')}/{quote(career_site, safe='')}/jobs"
    ).strip()
    detail_template = str(
        target.options.get("detail_path_template")
        or "/wday/cxs/{tenant}/{career_site}/{external_path}"
    ).strip()
    if "{external_path}" not in detail_template:
        raise SourceConfigurationError(
            "Workday detail_path_template must contain {external_path}."
        )
    return _WorkdayConfig(
        base_url=base_url,
        tenant=tenant,
        career_site=career_site,
        locale=locale,
        search_path=search_path,
        detail_path_template=detail_template,
    )


def _page_size(value: Any) -> int:
    try:
        return min(20, max(1, int(value or 20)))
    except (TypeError, ValueError):
        return 20


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text_value(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("descriptor", "label", "name", "value"):
            if value.get(key) not in (None, ""):
                return str(value[key]).strip()
        return ""
    return str(value or "").strip()


def _path_identifier(value: str) -> str:
    path = urlsplit(value).path.rstrip("/")
    if not path:
        return ""
    name = path.rsplit("/", 1)[-1]
    match = re.search(r"_([^_/?#]+)$", name)
    return match.group(1) if match else name


def _request_error_type(exc: SourceRequestError) -> str:
    if re.search(r"HTTP (401|403|404|405)\b", exc.reason):
        return "UnsupportedWorkdayPattern"
    return type(exc).__name__
