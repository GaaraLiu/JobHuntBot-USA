"""Browser-rendered, read-only application form extraction."""

from .ats_detection import detect_rendered_ats
from .base import (
    BROWSER_STATUSES,
    BrowserFieldSnapshot,
    BrowserRenderedForm,
    BrowserSessionPolicy,
    ReadOnlyBrowserBackend,
    validate_public_browser_url,
)
from .extraction import BrowserExtractionOutcome, BrowserFormExtractionService
from .playwright_browser import BrowserDependencyError, PlaywrightReadOnlyBrowser

__all__ = [
    "BROWSER_STATUSES",
    "BrowserDependencyError",
    "BrowserExtractionOutcome",
    "BrowserFieldSnapshot",
    "BrowserFormExtractionService",
    "BrowserRenderedForm",
    "BrowserSessionPolicy",
    "PlaywrightReadOnlyBrowser",
    "ReadOnlyBrowserBackend",
    "detect_rendered_ats",
    "validate_public_browser_url",
]
