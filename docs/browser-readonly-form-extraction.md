# Browser-rendered application form extraction

Phase 3.2 renders public application pages in an isolated browser and reads
their DOM structure. It does not fill, upload, check, select, sign, or submit
anything. The browser layer feeds the existing Phase 3.1 `ApplicationForm`,
fingerprint, question mapper, answer-safety policy, and `ApplicationMappingPlan`.

## Installation

Browser support is optional so ordinary tests and Phase 1–3.1 workflows do not
download browser software:

```text
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

The first command installs the Python Playwright package. The second explicitly
installs Chromium and may be a large download; it is never run by unit tests.
Managed environments that already provide a compatible full Chromium can set
`JOBHUNTBOT_PLAYWRIGHT_EXECUTABLE` to that executable instead of downloading a
second browser.

## Safety boundary

Every inspection uses a new non-persistent browser and context. It does not use
a Chrome/Edge profile, import cookies, persist storage state, accept downloads,
or retain credentials. Browser requests are limited to `GET`, `HEAD`, and
`OPTIONS`; `POST`, `PUT`, `PATCH`, `DELETE`, and other mutation methods are
aborted and recorded as method/path-only audit metadata.

The backend intentionally contains no calls to Playwright input or interaction
APIs such as `fill`, `type`, `click`, `check`, `select_option`, or
`set_input_files`. A public Apply link may be followed only by navigating
directly to its public HTTP(S) `href`. Buttons without a public link are not
activated.

CAPTCHA/anti-bot challenges produce `BLOCKED_BY_ANTI_BOT`. Password/account
walls produce `LOGIN_REQUIRED`. Neither is bypassed. A visible Next/Continue
control is recorded, but never activated; the result records
`later_steps_unavailable_without_submission = true`.

Network auditing stores only sanitized URL scheme/host/path, method, resource
type, and status. Query strings, request bodies, headers, cookies, tokens, and
response bodies are not retained.

## Extraction

The rendered DOM extractor reads:

- native input, textarea, select, radio, checkbox, date, email, phone, and file
  controls;
- ARIA combobox/radio/checkbox structures and already-rendered options;
- labels, accessible names, placeholders, help text, required/disabled/hidden
  state, validation hints, section headings, repeat groups, and stable
  structural locators;
- current-step markers and the presence of later-step controls.

It never reads control values, so browser autofill or personal form data cannot
be copied into a snapshot. The structural result is converted through the
Phase 3.1 generic structured adapter, and browser metadata is attached to the
existing canonical fields. The Phase 3.1 fingerprint remains the only form
fingerprint.

## CLI

```text
python -m jobhuntbot.cli inspect-application-form \
  --url https://public.example/application/123 \
  --output-dir my-materials/application/browser_forms \
  --headless --json
```

The command writes real artifacts only under ignored `my-materials/` paths:

```text
my-materials/application/browser_forms/
my-materials/application/browser_snapshots/
my-materials/application/browser_logs/
```

To generate a local mapping plan after extraction, provide both
`--application-profile` and `--answer-bank`; `--application-package` may provide
job and selected-resume context. Those facts are used locally after DOM
extraction and are never injected into the browser.

The CLI always emits this warning before starting:

```text
READ ONLY — NO FORM DATA WILL BE ENTERED, UPLOADED, OR SUBMITTED.
```

## Limitations

Some ATS application schemas require mutation-style GraphQL requests, account
creation, candidate data, or an anti-bot challenge. They remain blocked or
partial. This is intentional: incomplete extraction is preferable to weakening
the read-only boundary.
