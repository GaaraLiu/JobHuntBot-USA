# Phase 1 Architecture

## Scope

Phase 1 is a local matching core. Given a validated candidate profile and raw job-description text, it produces normalized job data, an evidence-backed score, APPLY/REVIEW/SKIP, requirement lists, explicit hard blockers, and a structured resume recommendation. Saving to the CSV dashboard is optional.

It does not scrape job sites, control a browser, authenticate, bypass verification, submit applications, rewrite resumes, call an LLM, or use a database.

## Pipeline

```text
RawJob
  -> DeterministicJobParser
  -> NormalizedJob + deterministic job_id
  -> JobFitScorer
  -> APPLY / REVIEW / SKIP
  -> ResumeRouter (structured JSON rules)
  -> optional canonical job_pool.csv upsert
```

The parser implements a small interface so later source-specific or optional parsing adapters can produce the same normalized model without changing scoring, routing, storage, or CLI code.

## Scoring behavior

Each component reports its maximum points, awarded points, reason, evidence, and status. Unknown components use `awarded_points: null`; they are excluded from the assessed-score denominator and reduce evidence coverage. APPLY also requires the configured minimum coverage.

Hard blockers are evaluated outside the numeric score. A conflict is created only when both the job and candidate profile contain enough explicit evidence. Unknown work authorization, citizenship, license, or clearance facts remain unknown and cap an otherwise-APPLY result at REVIEW; they are not blockers.

## Resume routing

Executable routing reads JSON with resume ID/path, target job families, keywords, skills, industries, priority, and active state. Candidate-confirmed skills are intersected with job requirements before contributing routing evidence. Markdown resume-routing files remain human-readable planning documents and are never parsed by the core.

## Configuration

Default thresholds, component weights, skill aliases, parser terms, industries, and job-family keywords live in `src/jobhuntbot/resources/default_config.json`. A user JSON file can deep-override selected values through the CLI's `--config` argument.
