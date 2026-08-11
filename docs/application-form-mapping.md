# Application form detection and mapping

Phase 3.1 is a read-only boundary. It can parse a saved HTML or JSON form,
normalize its fields, and build a private mapping plan. It does not fetch a
page, fill a field, upload a file, accept an attestation, or submit an
application.

## Canonical model

`ApplicationForm` contains ATS identity, job context, detection status,
sections, blockers, and a deterministic structural fingerprint. Every
`ApplicationField` preserves its source identifier, section, label, type,
required flag, choices, validation metadata, repeat-group position, canonical
question mapping, mapping confidence, and safety class.

Supported field types include text, textarea, email, phone, number, boolean,
select, multiselect, radio, checkbox, date, file, address, resume,
cover-letter, consent, signature, and unknown.

The common adapter interface has dedicated parsers for Greenhouse, Lever,
Ashby, SmartRecruiters, and Workday. It accepts employer-provided/saved HTML,
saved structured metadata, or manually supplied question JSON. Unknown ATS
sources remain `unknown`; no identity is invented.

## Mapping and safety

Mappings use exact or tightly bounded deterministic patterns and have one of
four confidence values: `HIGH`, `MEDIUM`, `LOW`, or `UNMAPPED`. Only a
high-confidence mapping to a confirmed static fact can be marked
`AUTO_READY_FUTURE`. That action is a future eligibility annotation, not an
instruction to fill a form.

The Phase 3.0 answer-bank safety class remains authoritative. Mapping
confidence never reduces it. In particular:

- citizenship, permanent residence, work authorization, and sponsorship are
  separate concepts;
- security clearance, licenses, and unknown experience are never guessed;
- salary remains job-dependent and may require confirmation;
- voluntary demographic questions remain isolated and may use the existing
  prefer-not-to-answer policy only when optional;
- consent, signature, background-check, arbitration, privacy, and accuracy
  attestations are always manual;
- resume, cover-letter, transcript, portfolio, and other attachment fields are
  detected structurally but are never uploaded.

Repeated education, employment, and certification groups retain their repeat
index. The mapper can point to a corresponding private profile collection, but
keeps group alignment reviewable.

## Mapping-plan actions

- `AUTO_READY_FUTURE`: high-confidence, confirmed, static candidate fact that
  a later explicitly authorized phase could fill.
- `USER_CONFIRMATION`: mapped, but job-dependent, review-sensitive, or not
  high-confidence enough for future automatic use.
- `MANUAL_ONLY`: file, consent, signature, or legal/manual interaction.
- `UNRESOLVED`: required information is unavailable or unmapped.
- `OPTIONAL_SKIP`: optional and unresolved, demographic-prefer-not, or safely
  skippable.

Any required unresolved, confirmation, or manual-only field prevents `READY`.
A failed/blocked detection makes the plan `BLOCKED`.

## CLI

Both commands operate on local files and have no network or interaction code:

```text
jobhuntbot application-form-inspect --input saved-form.json \
  --application-url https://example.invalid/job/123 --ats auto \
  --output my-materials/application/forms/example.json --json

jobhuntbot application-form-map \
  --form my-materials/application/forms/example.json \
  --application-profile my-materials/application/candidate_application_profile.json \
  --answer-bank my-materials/application/answer_bank.json \
  --output my-materials/application/mappings/example.json --json
```

Real form structures and plans must be written under
`my-materials/application/forms/`, `mappings/`, or `form_fixtures/`. Tracked
test fixtures contain fictional data only.

## Fingerprint

The SHA-256 fingerprint includes ATS, section identity, repeat metadata, field
IDs, normalized labels, field types, required flags, and choices in their
detected order. Candidate answer values, profile facts, and answer-bank values
are not inputs.
