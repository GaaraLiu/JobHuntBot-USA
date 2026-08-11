# Phase 3.0: Application preparation

Phase 3.0 prepares private, reviewable application packages. It does not open
websites, log in, inspect live forms, upload files, or submit applications. It
consumes the existing Phase 1/2 decision and selected resume unchanged.

## Private files

Candidate-specific files belong below `my-materials/application/`:

```text
answer_bank.json
candidate_application_profile.json
application_queue.json
packages/
unresolved/
history/
logs/
```

The tracked templates contain fictional placeholders only. Real candidate
values, employer questions, packages, and history stay in the ignored private
tree.

## Safety model

Every candidate fact carries a value, status, provenance, verification date,
and notes. Confirmed facts require provenance. Conflicting sources stay
unresolved. The answer bank separates static confirmed, job-dependent,
confirmation-required, optional, manual-only, unknown, and never-guess
answers. Citizenship is never derived from authorization, and experience is
never calculated from unrelated dates or projects.

Packages are `READY`, `NEEDS_USER_INPUT`, `BLOCKED`, or `INVALID`. Queue status
may reach `PACKAGE_READY`, but this phase has no `SUBMITTED` state.

## CLI workflow

```powershell
jobhuntbot application-queue-import --input <private-results.json>
jobhuntbot application-queue-list
jobhuntbot validate-answer-bank `
  --application-profile my-materials/application/candidate_application_profile.json `
  --answer-bank my-materials/application/answer_bank.json
jobhuntbot answer-bank-unresolved `
  --application-profile my-materials/application/candidate_application_profile.json `
  --answer-bank my-materials/application/answer_bank.json `
  --questions <private-structured-questions.json>
jobhuntbot application-package-build `
  --application-id <application-id> `
  --application-profile my-materials/application/candidate_application_profile.json `
  --answer-bank my-materials/application/answer_bank.json `
  --questions <private-structured-questions.json>
```

Only `APPLY` imports by default. `REVIEW` requires an explicit
`--promote-review-job-id`; its original recommendation remains in the snapshot.
`SKIP` is always excluded. A stable job ID deduplicates repeated input. A
materially changed job is retained as `pending_update`, while the existing
queue record moves to `NEEDS_REVIEW` rather than being overwritten.
