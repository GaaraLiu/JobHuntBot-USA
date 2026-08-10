# Dashboard Data Schema

## Canonical source

`schemas/dashboard.schema.json` is the canonical CSV contract. Its top-level `schema_version` is currently `1`. Code and tests must use its exact column order instead of maintaining independent hard-coded copies.

This phase does not include a general migration framework. When a later phase changes the schema, it should increment `schema_version`, document the change, and add a focused migration before updating the live CSV headers.

## Ownership and relationships

- `job_pool.csv` owns normalized job facts, the full `job_description`, fit output, and resume recommendation.
- `application_log.csv` owns facts about a particular application attempt. It references the job through `job_id` and does not duplicate the full job description.
- `blocker_queue.csv` and `follow_up.csv` reference `job_id` when it is available.
- Legacy rows without `job_id` remain displayable through the dashboard's company-and-title fallback.

The normalized model maps to legacy dashboard names as follows:

| Normalized field | Dashboard field |
|---|---|
| `title` | `job_title` |
| `discovered_date` | `date_found` |
| `apply_url` or `source_url` | `job_url` compatibility alias |

## Structured CSV cells

Lists and objects are serialized as canonical JSON with UTF-8, sorted object keys, and compact separators. CSV writing uses the standard CSV parser/writer, quotes every field, and uses CRLF line endings. Round-trip tests cover commas, quotes, newlines, Unicode, and JSON decoding.

## Recommendation versus lifecycle status

`recommendation` expresses matching advice: `APPLY`, `REVIEW`, or `SKIP`.

`status` remains the workflow lifecycle field. A newly analyzed row maps as follows:

- `APPLY` → `Pending`
- `REVIEW` → `Needs user`
- `SKIP` → `Skipped`

Re-analysis does not downgrade an existing `Submitted`, `Offer`, or `Rejected` lifecycle state.

## Job ID behavior

IDs are deterministic and use this order:

1. Source-native job ID, namespaced by normalized source.
2. Canonical apply URL, otherwise canonical source URL.
3. A fingerprint of normalized company, title, location, and source.

Tracking parameters and fragments do not change a URL-based ID. A real canonical URL change does change the ID unless a stable source-native ID is available. Fingerprint collisions are unlikely but possible when two postings have identical fallback fields; future source adapters should provide native IDs whenever possible.

