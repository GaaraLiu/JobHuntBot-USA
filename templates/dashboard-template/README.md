# Dashboard Template

Use these CSV files as a lightweight dashboard. They can be imported into Excel, Google Sheets, Airtable, Notion, or converted into a workbook.

The machine-readable canonical field order is `schemas/dashboard.schema.json`. It has an explicit `schema_version`, and automated contract tests verify that both CSV copies match it. This README explains semantics but is not a second schema source.

## Sheet Purposes

### `daily_dashboard.csv`

Daily operator summary.

Fields:

- `date`: run date.
- `found_count`: jobs found.
- `submitted_count`: confirmed submissions only.
- `skipped_count`: intentionally skipped jobs.
- `blocked_count`: attempted but automation could not complete.
- `needs_user_count`: waiting for user action.
- `pending_count`: queued for later.
- `top_sources`: LinkedIn, company sites, job boards, etc.
- `summary`: short run summary.
- `user_actions_needed`: concrete next actions for the user.

### `job_pool.csv`

Main job lead table and current state.

Fields:

- `date_found`: when the job was found.
- `company`, `job_title`, `role_family`, `level`, `location`, `remote_policy`.
- `source`, `job_url`, `posted_date`.
- `priority`: High, Medium, Low, Stretch.
- `status`: Submitted, Skipped, Blocked, Needs user, Pending.
- `resume_variant`: selected resume route.
- `skip_reason`: why skipped.
- `blocker`: current blocker if any.
- `next_action`: what should happen next.
- `notes`: short context.
- `cohort_match_status`: `Yes` / `No` / `Unclear` — whether the target hiring cycle (e.g. a specific graduating class / 届 for campus recruiting) has been explicitly confirmed open for this row. Set this explicitly every time you finish checking a company; don't leave the dashboard to guess it from free-text `notes`. The dashboard's "confirmed open, not yet applied" view is driven entirely by this column.
- `current_stage`: free-text label for where an already-submitted application currently stands (e.g. "笔试", "二面"). Set by the dashboard's calendar "add/edit event" action, verbatim as typed — it does not add a suffix or normalize wording, since every company's process reads differently. Left blank until the first calendar event is scheduled for that job.

Phase 1 appends normalized matching fields including `job_id`, source/apply URLs, `job_description`, parsed salary/experience/education/skills, `fit_score`, `score_coverage`, `recommendation`, explainable score details, requirement lists, hard blockers, and the recommended resume. Structured lists and objects use deterministic JSON inside properly quoted CSV cells.

`job_pool.csv` is the sole owner of the full `job_description`. `job_url` remains a dashboard-compatible alias: it contains `apply_url` when present, otherwise `source_url`.

### `application_log.csv`

Audit trail for actual attempts.

Fields:

- `attempt_date`, `company`, `job_title`, `job_url`, `platform`.
- `status`: final attempt outcome.
- `submission_evidence`: what proved submission.
- `resume_used`: exact resume file or variant.
- `answers_used`: answer bank sections or custom answers used.
- `confirmation_url`, `confirmation_text`.
- `notes`.
- `job_id`: stable reference to the matching `job_pool` row. Application-specific rows do not duplicate `job_description`.

### `blocker_queue.csv`

Retry and handoff queue.

Fields:

- `date`, `company`, `job_title`, `job_url`.
- `blocker_category`: CAPTCHA, login, dropdown, upload, missing material, etc.
- `what_happened`: observed failure.
- `why_blocked`: why the agent stopped.
- `can_retry`: yes/no.
- `next_retry_strategy`: browser automation, visual control, user handoff, skip.
- `user_action_needed`: exact user action.
- `status`: open, retry later, resolved, abandoned.
- `job_id`: stable link to the related `job_pool` row when known.

### `follow_up.csv`

Post-application pipeline.

Fields:

- `date`, `time`, `company`, `job_title`, `contact`, `channel`.
- `event_type`: recruiter reply, rejection, interview, assessment, follow-up. Rows added via the dashboard's calendar UI use this field as free-text event content and also copy it into `job_pool.csv`'s `current_stage` for that job.
- `deadline`, `next_action`, `status`, `notes`.
- `job_id`: stable link to the related `job_pool` row when known.

### `resume_rules.csv`

Resume routing table.

Fields:

- `role_family`.
- `resume_file_path`.
- `use_for_titles`.
- `avoid_for_titles`.
- `tailor_threshold`: external score or qualitative threshold.
- `notes`.

### `automation_rules.csv`

Lessons learned.

Fields:

- `date`.
- `rule_category`: screening, browser, ATS, answer, resume, safety.
- `rule`: new or updated rule.
- `reason`: why it exists.
- `source_blocker_or_lesson`: link to blocker, application, or observation.
- `status`: active, testing, retired.

## Counting Rules

- Count only confirmed submissions in `submitted_count`.
- Put every job in `job_pool.csv` once.
- Put every real application attempt in `application_log.csv`, even if it failed.
- For lead-finding-only trials, leave `application_log.csv` empty because no application attempt occurred.
- Put repeatable failures in `blocker_queue.csv`.
- Convert repeated blockers into `automation_rules.csv`.

## Lead-Finding Trial Rules

For a first clean-room trial or demo:

- Find 3-5 jobs.
- Update `job_pool.csv` and `daily_dashboard.csv`.
- Use `Pending` only for jobs worth later review or application.
- Use `Needs user` when a missing high-impact fact blocks the decision, such as sponsorship, work authorization, compensation, relocation, or real resume file.
- Use `Skipped` for roles that clearly violate rules.
- Use `Blocked` only when an attempted workflow or site interaction cannot proceed.
- Do not write to `application_log.csv` unless the agent actually opened or attempted an application flow.
