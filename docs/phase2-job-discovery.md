# Phase 2: U.S. job discovery

Phase 2 retrieves public employer postings from supported ATS interfaces, keeps their source evidence, deterministically removes duplicates, and sends each full job description through the frozen Phase 1 parser, scorer, and resume router. It does not automate applications.

## Supported public sources

- SmartRecruiters Posting API: `https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings`
- Greenhouse Job Board API: `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
- Lever Postings API: `https://api.lever.co/v0/postings/{site}?mode=json`
- Ashby public job board API: `https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true`

Only public, employer-provided listing interfaces are used. The HTTP client has a descriptive User-Agent, a timeout, bounded retries, and configurable polite pacing. It does not bypass authentication, rate limits, CAPTCHAs, or access controls.

## Configuration

Copy `templates/discovery_config.template.json` to the ignored private path `my-materials/discovery/discovery_config.json`. A target has a supported `source`, its ATS `board` identifier, optional display `company`, `enabled`, optional `job_families` and `search_keywords`, optional `location_filter`, optional `employment_types`, and optional `max_results`.

The four initial search families are `higher_education`, `data_bi`, `urban_transport_gis`, and `applied_ai`. Their keyword lists are discovery filters only; they do not alter Phase 1 job-family parsing or scoring. An absent ATS location or employment type remains unknown and is retained rather than guessed. Candidate location policy remains in the private candidate profile/config and is evaluated by Phase 1.

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

Repeat `--source greenhouse` (or another supported source) to limit sources. Add `--limit 10` for a bounded run. Add `--discovery-only` to retrieve, filter, deduplicate, and normalize without scoring or resume routing. Add `--json` for a machine-readable run summary.

## Private outputs

The default output root is ignored `my-materials/discovery/`:

- `raw/`: adapter records, including source metadata
- `normalized/`: canonical Phase 1 `NormalizedJob` JSON
- `results/`: complete Phase 1 score and resume-route JSON
- `discovery_log.jsonl`: source failures, job failures, duplicate reasons, and run summaries
- `job_pool.csv`: the current run ranked by decision, score, and evidence coverage

All APPLY, REVIEW, and SKIP results are retained. Real discovery never writes `dashboard/job_pool.csv` by default. One source or posting failure is logged without aborting other targets.

## Out of scope

This phase contains no LinkedIn, Indeed, Workday, browser automation, authentication bypass, form filling, resume upload, application submission, or auto-submit behavior.
