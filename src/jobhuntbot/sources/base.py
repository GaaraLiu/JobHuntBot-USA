"""Source-independent discovery contracts and polite JSON HTTP access."""

from __future__ import annotations

import html
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..models import RawJob


class SourceConfigurationError(ValueError):
    """Raised when a discovery target is incomplete or unsupported."""


class SourceRequestError(RuntimeError):
    """Raised when a public ATS request cannot be completed safely."""

    def __init__(self, url: str, reason: str):
        super().__init__(reason)
        self.url = url
        self.reason = reason


@dataclass(slots=True)
class SourceSalary:
    minimum: float | None = None
    maximum: float | None = None
    currency: str = ""
    period: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SourceJob:
    source: str
    source_job_id: str
    company: str
    title: str
    description: str
    location: str = ""
    work_mode: str = ""
    employment_type: str = ""
    posted_date: str = ""
    updated_date: str = ""
    salary: SourceSalary | None = None
    source_url: str = ""
    apply_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_raw_job(self, *, discovered_date: str = "") -> RawJob:
        source_fields = {
            "work_mode": self.work_mode,
            "employment_type": self.employment_type,
            "salary": None if self.salary is None else self.salary.to_dict(),
            "updated_date": self.updated_date,
        }
        return RawJob(
            title=self.title,
            company=self.company,
            location=self.location,
            source=self.source,
            source_native_id=self.source_job_id,
            source_url=self.source_url,
            apply_url=self.apply_url,
            posted_date=self.posted_date,
            discovered_date=discovered_date,
            job_description=self.description,
            metadata={
                "discovery_source_fields": source_fields,
                "source_metadata": self.metadata,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["salary"] = None if self.salary is None else self.salary.to_dict()
        return value


@dataclass(slots=True)
class SourceFailure:
    source: str
    url: str = ""
    job_id: str = ""
    error_type: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class SourceBatch:
    jobs: list[SourceJob] = field(default_factory=list)
    errors: list[SourceFailure] = field(default_factory=list)
    pages_fetched: int = 0


@dataclass(slots=True)
class DiscoveryTarget:
    source: str
    board: str
    company: str = ""
    enabled: bool = True
    search_keywords: list[str] = field(default_factory=list)
    location_filter: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    job_families: list[str] = field(default_factory=list)
    max_results: int | None = None
    max_pages: int = 1
    posted_within_days: int | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DiscoveryTarget":
        known = {
            "source",
            "board",
            "company",
            "enabled",
            "search_keywords",
            "location_filter",
            "employment_types",
            "job_families",
            "max_results",
            "max_pages",
            "posted_within_days",
        }
        source = str(value.get("source", "")).strip().casefold()
        board = str(value.get("board", value.get("company_identifier", ""))).strip()
        if not source:
            raise SourceConfigurationError("Discovery target requires source.")
        if not board:
            raise SourceConfigurationError(
                f"Discovery target for source '{source}' requires a board/company identifier."
            )
        max_results = _optional_positive_int(value.get("max_results"), "max_results")
        max_pages = _optional_positive_int(value.get("max_pages", 1), "max_pages") or 1
        posted_within_days = _optional_positive_int(
            value.get("posted_within_days"), "posted_within_days"
        )
        return cls(
            source=source,
            board=board,
            company=str(value.get("company", "")).strip(),
            enabled=value.get("enabled", True) is not False,
            search_keywords=_strings(value.get("search_keywords")),
            location_filter=_strings(value.get("location_filter")),
            employment_types=_strings(value.get("employment_types")),
            job_families=_strings(value.get("job_families")),
            max_results=max_results,
            max_pages=max_pages,
            posted_within_days=posted_within_days,
            options={key: item for key, item in value.items() if key not in known},
        )


class JsonHttpClient(Protocol):
    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        ...


class SourceAdapter(Protocol):
    source: str

    def fetch(self, target: DiscoveryTarget, *, limit: int | None = None) -> SourceBatch:
        ...


class PoliteJsonClient:
    """Small standard-library client with bounded retries and polite pacing."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        retries: int = 2,
        min_interval_seconds: float = 0.5,
        user_agent: str = "JobHuntBot/0.2 (local public-job discovery)",
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        if retries < 0:
            raise ValueError("retries cannot be negative.")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative.")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.min_interval_seconds = min_interval_seconds
        self.user_agent = user_agent
        self._last_request_at: float | None = None

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        request_headers.update(dict(headers or {}))
        last_reason = ""
        for attempt in range(self.retries + 1):
            self._pace()
            request = Request(url, headers=request_headers, method="GET")
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read()
                self._last_request_at = time.monotonic()
                return json.loads(payload.decode("utf-8-sig"))
            except HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_reason = f"HTTP {exc.code}: {exc.reason}"
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= self.retries:
                    raise SourceRequestError(url, last_reason) from exc
            except (URLError, TimeoutError) as exc:
                self._last_request_at = time.monotonic()
                last_reason = str(getattr(exc, "reason", exc))
                if attempt >= self.retries:
                    raise SourceRequestError(url, last_reason) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._last_request_at = time.monotonic()
                raise SourceRequestError(url, f"Invalid JSON response: {exc}") from exc
            if attempt < self.retries:
                time.sleep(min(2**attempt, 4))
        raise SourceRequestError(url, last_reason or "Request failed.")


class _TextExtractor(HTMLParser):
    block_tags = {
        "article",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def html_to_text(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(raw))
    parser.close()
    return parser.text()


def join_description_sections(sections: list[tuple[str, Any]]) -> str:
    values: list[str] = []
    for heading, body in sections:
        text = html_to_text(body)
        if not text:
            continue
        if heading:
            values.append(str(heading).strip())
        values.append(text)
    return "\n\n".join(values).strip()


def normalize_work_mode(value: Any) -> str:
    key = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    if key in {"remote", "fully-remote"}:
        return "remote"
    if key in {"hybrid", "hybrid-remote"}:
        return "hybrid"
    if key in {"onsite", "on-site"}:
        return "onsite"
    return ""


def normalize_employment_type(value: Any) -> str:
    key = "".join(character for character in str(value or "").casefold() if character.isalnum())
    aliases = {
        "fulltime": "full-time",
        "permanent": "full-time",
        "parttime": "part-time",
        "contract": "contract",
        "contractor": "contract",
        "temporary": "temporary",
        "temp": "temporary",
        "intern": "internship",
        "internship": "internship",
    }
    return aliases.get(key, str(value or "").strip().casefold())


def normalize_posted_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return ""


def location_from_parts(*parts: Any) -> str:
    values = [str(item).strip() for item in parts if str(item or "").strip()]
    return ", ".join(dict.fromkeys(values))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]

def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise SourceConfigurationError(f"{field_name} must be a positive integer or null.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceConfigurationError(
            f"{field_name} must be a positive integer or null."
        ) from exc
    if parsed < 1:
        raise SourceConfigurationError(f"{field_name} must be positive.")
    return parsed
