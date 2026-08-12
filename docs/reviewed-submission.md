# Human-reviewed application submission

Phase 3.4 adds a deliberately separate review, approval, and execution layer.
It does not turn discovery or autofill into unattended submission. The default
execution mode is still dry run.

```text
ApplicationPackage + ApplicationForm + MappingPlan + ControlledFillPlan
  -> ReviewPackage
  -> SubmissionApproval
  -> explicit execute-application --submit
  -> SubmissionResult
  -> private metadata history / accurate queue status
```

## Review and approval

`ReviewPackage` shows the job, selected resume, score when available, answer
concepts, safe answer descriptions, provenance, salary/application-specific
states, requested documents, manual items, unresolved required questions, and
blockers. Sensitive identity/contact/address/authorization values are described
without being duplicated. The review itself grants no approval.

`SubmissionApproval` is bound to all of the following:

- application ID;
- ApplicationForm fingerprint;
- material ApplicationPackage fingerprint and package version;
- exact selected resume ID, canonical path, size, and SHA-256;
- reviewed application origin;
- individually resolved legal/manual item IDs.

Changing the package, form, origin, or resume content makes approval `EXPIRED`.
Only `APPROVED_FOR_SUBMISSION` may open the submission firewall. General
approval never clicks or accepts legal attestations.

## Documents

The exact package-selected resume must be a readable PDF with a PDF signature
and satisfy declared type/size constraints. No alternate resume is selected.
A required cover letter without an approved PDF blocks approval. An optional
cover letter is skipped when no approved document exists.

## CAPTCHA and login

CAPTCHA produces `PAUSED_FOR_CAPTCHA`; login/account walls produce
`PAUSED_FOR_LOGIN`. The software never solves CAPTCHA, creates an account,
enters a password, or stores credentials. A user may complete such a step
manually outside the automation boundary, after which the form and approval
must be revalidated. The current implementation does not preserve a paused
browser session between CLI runs.

## Scoped firewall and navigation

The firewall remains locked during initial navigation and form verification.
After exact submission approval, it permits only POST/PUT/PATCH requests for
the approved application ID and exact application origin. DELETE and mutations
to other origins remain blocked. Request bodies and values are never logged.

Non-final `Next`, `Continue`, and `Save and Continue` controls are distinct
from final submission. A newly discovered question or materially changed
field stops execution with review required. Final submission requires exactly
one positive final label (`Submit Application`, `Submit`, `Apply`, or
`Send Application`) in the verified form context. Generic or ambiguous
controls stop for review.

`SUBMITTED_CONFIRMED` requires positive confirmation text or a confirmation URL
pattern. A completed click without confirmation is
`SUBMITTED_UNCONFIRMED`; it is never upgraded by assumption.

## CLI

Review only:

```text
python -m jobhuntbot.cli review-application \
  --application-id application_example \
  --application-package my-materials/application/packages/application_example.json \
  --mapping-plan my-materials/application/mappings/application_example.json \
  --application-form my-materials/application/browser_forms/application_example.json \
  --browser-snapshot my-materials/application/browser_snapshots/application_example.json
```

Record explicit approval without opening a browser:

```text
python -m jobhuntbot.cli approve-application \
  --application-id application_example \
  --review-package my-materials/application/reviews/application_example_review.json \
  --application-package my-materials/application/packages/application_example.json \
  --application-form my-materials/application/browser_forms/application_example.json \
  --for-submission --confirm-reviewed
```

Dry-run validation is the default:

```text
python -m jobhuntbot.cli execute-application ...
```

An actual execution requires the separate `--submit` flag and a valid
`APPROVED_FOR_SUBMISSION` artifact. Duplicate confirmed submissions are blocked
unless `--allow-duplicate-submission` is explicitly supplied.

## Private artifacts

All real artifacts remain ignored under:

```text
my-materials/application/reviews/
my-materials/application/approvals/
my-materials/application/submissions/
```

History contains company/title/ATS/job/application IDs, timestamps, and status,
not candidate answers, passwords, resume contents, or request bodies.
