# Phase 2: U.S. job discovery

Phase 2 retrieves public employer postings from supported ATS interfaces, keeps their source evidence, and deterministically removes duplicates. Phase 2.1 applies frozen Phase 1 normalization (including seniority), then adds an explainable relevance gate and private incremental state before scoring and resume routing. It does not automate applications.

## Supported public sources

- SmartRecruiters Posting API: `https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings`
- Greenhouse Job Board API: `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
- Lever Postings API: `https://api.lever.co/v0/postings/{site}?mode=json`
- Ashby public job board API: `https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true`

Only public, employer-provided listing interfaces are used. The HTTP client has a descriptive User-Agent, a timeout, bounded retries, and configurable polite pacing. It does not bypass authentication, rate limits, CAPTCHAs, or access controls.

## Configuration

Copy `templates/discovery_config.template.json` to the ignored private path `my-materials/discovery/discovery_config.json`. A target has a supported `source`, its ATS `board` identifier, optional display `company`, `enabled`, optional career-track restrictions in `job_families`, explicit `search_keywords`, optional `location_filter`, optional `employment_types`, `max_pages`, `max_results`, and optional `posted_within_days`.

The four initial tracks are `higher_education`, `data_bi`, `urban_transport_gis`, and `applied_ai`. Each track can configure title aliases, title/category/description evidence, positive terms, exclusion terms, and advanced-seniority exceptions. Title and category evidence have higher deterministic weights than description evidence. Generic words such as `data`, `analysis`, `technology`, `business`, `research`, and `engineering` are explicitly prevented from establishing relevance on their own. These settings do not alter Phase 1 job-family parsing or scoring.

`location_policy` is a private discovery gate for `allowed_countries` (U.S.-only by default), primary markets, U.S. remote terms, relocation markets, exclusions, and whether other confirmed U.S. locations remain reviewable. Explicit ATS country metadata takes precedence; otherwise deterministic country/state evidence is used. Known non-U.S. jobs are `international` and excluded unless their country is explicitly allowed. Ambiguous locations remain `unknown`. The tracked template contains no private cities. The optional freshness filter uses a source's explicit updated date, then posted date; a missing or unparseable date remains `UNKNOWN` and is retained.

SmartRecruiters and Lever use bounded server pagination (`offset` and `skip` respectively). Greenhouse and Ashby publish their current job-board collection in one response, so those adapters use one request and apply a client-side result cap. Every target is bounded by `max_pages`, `max_results`, and the global fetch cap.

The template targets are disabled placeholders. Do not commit real board targets, credentials, candidate data, or discovered postings.

## Normalization precedence

1. Every adapter retains the source name, native ID, company, title, full description, location, URLs, and any explicitly published optional fields.
2. The complete description is always parsed by the existing deterministic Phase 1 parser; `NormalizedJob` remains canonical.
3. ATS location is supplied as the raw job location. Explicit structured ATS work mode, employment type, and numeric salary range take precedence only for the same normalized field and are recorded in `parser_evidence`.
4. Empty adapter fields never erase JD-derived values. Missing facts remain empty/null; no value is reconstructed or fabricated.

## Stable identity and deduplication

Deduplication checks the same source plus native job ID first, then canonical apply/source URLs after tracking parameters and fragments are removed. The normalized company/title/location fingerprint is used only when both native ID and canonical URLs are absent. The first record is retained, missing optional fields may be filled from its duplicate, and every merge is written to `discovery_log.jsonl` with its exact reason. Effective normalized job IDs continue to use the existing Phase 1 `build_job_id` policy.

## CLI

Install the local package, then run:

```bash
python -m jobhuntbot discover \
  --profile my-materials/candidate_profile.json \
  --resume-routing my-materials/resume_routing.json \
  --scoring-config my-materials/config/jobhuntbot.local.json \
  --discovery-config my-materials/discovery/discovery_config.json \
  --output-dir my-materials/discovery
```

Repeat `--source greenhouse` (or another supported source) to limit sources. Add `--limit 10` for a bounded run. Add `--discovery-only` to retrieve, filter, deduplicate, normalize, and evaluate relevance without scoring or resume routing. Add `--reanalyze-unchanged` only when an explicit repeat analysis is needed. Add `--json` for a machine-readable run summary.

When `incremental.enabled` is true, stable `job_id` plus a deterministic content fingerprint drives analysis: new and changed jobs are analyzed, unchanged jobs reuse their private prior result, and a missing prior artifact triggers recovery analysis. The private JSON state records first/last seen, last analyzed, source/native ID, fingerprint, prior decision, resume, and latest relevance result. No database is required.

## Private outputs

The default output root is ignored `my-materials/discovery/`:

- `raw/`: adapter records, including source metadata
- `normalized/`: canonical Phase 1 `NormalizedJob` JSON
- `results/`: complete Phase 1 score and resume-route JSON for relevant analyzed jobs
- `recommended/`: current and per-job relevant records, including relevance and incremental explanations
- `irrelevant/`: current and per-job exclusions; raw evidence is retained and these jobs do not enter scoring
- `errors/`: current and per-error source/analysis failures
- `state/discovery_state.json`: private incremental state
- `discovery_log.jsonl`: source failures, job failures, duplicate reasons, and run summaries
- `job_pool.csv`: relevant current-run jobs ranked by decision, score, and evidence coverage

All APPLY, REVIEW, and SKIP results are retained. Real discovery never writes `dashboard/job_pool.csv` by default. One source or posting failure is logged without aborting other targets.

## Out of scope

This phase contains no LinkedIn, Indeed, Workday, browser automation, authentication bypass, form filling, resume upload, application submission, or auto-submit behavior.
