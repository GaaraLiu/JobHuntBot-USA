# Controlled application-form autofill

Phase 3.3 can populate a conservative subset of a verified browser-rendered
application form. It cannot submit an application. The flow is:

```text
ApplicationPackage + ApplicationMappingPlan + ApplicationForm
  + BrowserRenderedForm
    -> ControlledFillPlan
    -> optional explicit browser fill
    -> local field verification
    -> stop before submission
```

## Safety boundary

Preview is the default. Browser population requires `--execute-fill`.
`--allow-partial-fill` is also required when unresolved required fields remain.
Neither flag enables submission, file upload, legal consent, CAPTCHA bypass,
account creation, or login.

The browser uses a fresh context and installs two firewalls before navigation:

- a request firewall allows only `GET`, `HEAD`, and `OPTIONS`; it aborts and
  records sanitized `POST`, `PUT`, `PATCH`, and `DELETE` attempts;
- a submission firewall prevents submit events, the Enter-key submission path,
  `form.submit()`, and `requestSubmit()`.

The implementation contains no file-selection call and never activates a
Submit, Apply, Continue, Next, or arbitrary button. If the live form needs a
mutation request to remain functional, the result is `LIVE_FILL_UNSAFE`.

## Planning rules

A field becomes `FILL` only when its Phase 3.1 mapping is HIGH confidence and
autofill-eligible, the package contains a confirmed conflict-free value, the
category is allowlisted, and the Phase 3.2 locator/label/type/required/visibility
metadata still agrees. Available actions are `FILL`, `SKIP_OPTIONAL`,
`WAIT_FOR_USER`, `MANUAL_ONLY`, `BLOCKED`, and `UNRESOLVED`.

Initial allowlisted facts include legal name components and composition,
contact details, address, clearly requested profile links, explicitly confirmed
education/work-history facts, work authorization, sponsorship, driver's
license, and defensible confirmed domain-specific experience years.

Always-manual categories include files, signatures, attestations, legal or
privacy consent, arbitration, background-check consent, account/password
controls, CAPTCHA, and protected demographics. Total professional experience
remains unknown and is not calculated from dates.

Job-dependent answers are fillable only after resolution in the per-application
package. `Negotiable` may populate a text salary field after confirmation. A
numeric salary needs an explicit approved numeric package value. Otherwise the
action is `WAIT_FOR_USER`.

## Resume policy

The planner verifies that the selected resume ID points to the exact readable
PDF selected by the existing package. It records the intended private path.
Live file upload is disabled in Phase 3.3, even during explicit execution.

## CLI

```text
python -m jobhuntbot.cli autofill-application \
  --application-id application_example \
  --application-package my-materials/application/packages/application_example.json \
  --mapping-plan my-materials/application/mappings/application_example.json \
  --application-form my-materials/application/browser_forms/application_example.json \
  --browser-snapshot my-materials/application/browser_snapshots/application_example.json \
  --preview --json
```

Omit `--preview` for the same default preview behavior. Add `--execute-fill`
only after reviewing the plan. Preview and execution write value-minimized
artifacts under:

```text
my-materials/application/fill_plans/
my-materials/application/fill_results/
my-materials/application/fill_logs/
```

Normal plans and logs contain canonical concept IDs, actions, reasons, and
statuses, not candidate answer values. A result can be
`PREVIEW_READY`, `FILLED_SAFE_FIELDS`, `NEEDS_USER_INPUT`,
`BLOCKED_BY_CAPTCHA`, `FORM_CHANGED_REVIEW_REQUIRED`, `LIVE_FILL_UNSAFE`, or
`FAILED`. There is no `SUBMITTED` status, and the result model rejects
`submission_completed=true`.
