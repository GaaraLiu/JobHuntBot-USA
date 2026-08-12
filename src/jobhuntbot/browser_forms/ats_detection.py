"""Browser-rendered ATS detection that extends the Phase 3.1 URL identity."""

from __future__ import annotations

from typing import Iterable

from ..application_forms import detect_application_ats


_DOM_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workday", ("data-automation-id", "workday", "wd-icon")),
    ("greenhouse", ("greenhouse", "gh_jid", "application_form")),
    ("lever", ("lever", "posting-apply", "application-form")),
    ("ashby", ("ashby", "ashbyhq", "applicationform")),
    ("smartrecruiters", ("smartrecruiters", "oneclick-ui", "smartapply")),
)


def detect_rendered_ats(
    application_url: str,
    *,
    known_source: str = "",
    dom_markers: Iterable[str] = (),
) -> str:
    """Prefer existing Phase 2/3.1 identity, then deterministic DOM markers."""

    detected = detect_application_ats(application_url, known_source)
    if detected != "unknown":
        return detected
    joined = " ".join(str(item).casefold() for item in dom_markers)
    matches = [ats for ats, tokens in _DOM_SIGNATURES if any(token in joined for token in tokens)]
    return matches[0] if len(matches) == 1 else "unknown"
