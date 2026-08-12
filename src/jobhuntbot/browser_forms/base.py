"""Read-only browser extraction contracts and structural snapshot models."""

from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit


BROWSER_STATUSES = {
    "DETECTED",
    "PARTIAL",
    "NO_FORM_DETECTED",
    "LOGIN_REQUIRED",
    "BLOCKED_BY_ANTI_BOT",
    "DYNAMIC_BUT_INACCESSIBLE",
    "ERROR",
}


@dataclass(frozen=True, slots=True)
class BrowserSessionPolicy:
    """Non-persistent policy enforced by every browser backend."""

    headless: bool = True
    timeout_ms: int = 20_000
    allowed_http_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS")
    follow_public_apply_link: bool = True
    allow_manual_auth_handoff: bool = False
    max_public_navigations: int = 2
    persist_storage_state: bool = False
    accept_downloads: bool = False
    import_personal_profile: bool = False
    allow_file_upload: bool = False
    allow_form_input: bool = False
    allow_submit: bool = False

    def __post_init__(self) -> None:
        if self.timeout_ms < 1_000 or self.timeout_ms > 120_000:
            raise ValueError("Browser timeout must be between 1,000 and 120,000 ms.")
        if self.max_public_navigations not in {1, 2}:
            raise ValueError("Read-only extraction permits one page plus at most one public Apply navigation.")
        unsafe = {item.upper() for item in self.allowed_http_methods} - {"GET", "HEAD", "OPTIONS"}
        if unsafe:
            raise ValueError(f"Unsafe browser request methods are not permitted: {sorted(unsafe)}")
        if self.allow_manual_auth_handoff and self.headless:
            raise ValueError("Manual authentication requires a visible browser; use --headed with --manual-auth.")
        if any((self.persist_storage_state, self.accept_downloads, self.import_personal_profile,
                self.allow_file_upload, self.allow_form_input, self.allow_submit)):
            raise ValueError("Phase 3.2 browser policy cannot enable persistence, input, upload, or submission.")


@dataclass(slots=True)
class BrowserFieldSnapshot:
    field_id: str
    section_id: str
    section_label: str
    label: str
    accessible_name: str = ""
    dom_type: str = "unknown"
    required: bool = False
    options: list[str] = field(default_factory=list)
    placeholder: str = ""
    help_text: str = ""
    visible: bool = True
    hidden: bool = False
    disabled: bool = False
    locator: str = ""
    validation_rules: dict[str, Any] = field(default_factory=dict)
    repeat_group: str = ""
    repeat_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BrowserFieldSnapshot":
        allowed = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in allowed if key in value})


@dataclass(slots=True)
class BrowserRenderedForm:
    requested_url: str
    final_url: str
    page_title: str
    ats_hint: str
    status: str
    fields: list[BrowserFieldSnapshot] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    current_step: str = ""
    step_count: int | None = None
    later_steps_unavailable_without_submission: bool = False
    login_wall: bool = False
    anti_bot: bool = False
    apply_navigation_followed: bool = False
    blocked_request_count: int = 0
    public_network_metadata: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in BROWSER_STATUSES:
            raise ValueError(f"Unsupported browser extraction status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["fields"] = [item.to_dict() for item in self.fields]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BrowserRenderedForm":
        payload = dict(value)
        payload["fields"] = [BrowserFieldSnapshot.from_dict(item) for item in value.get("fields", [])]
        return cls(**payload)


class ReadOnlyBrowserBackend(Protocol):
    def inspect(self, url: str, policy: BrowserSessionPolicy) -> BrowserRenderedForm:
        """Navigate and inspect without entering, uploading, or submitting data."""


def validate_public_browser_url(url: str) -> str:
    """Reject credentials, local hosts, and literal private addresses."""

    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A public HTTP(S) application URL is required.")
    if parsed.username or parsed.password:
        raise ValueError("Application URLs containing credentials are not permitted.")
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Local browser targets are not permitted.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("Private, loopback, and link-local browser targets are not permitted.")
    return parsed.geturl()
