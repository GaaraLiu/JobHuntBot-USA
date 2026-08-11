"""Phase 2.3 orchestration from aggregator leads to authoritative ATS jobs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .aggregators import (
    AGGREGATOR_SOURCES,
    AggregatorAdapter,
    AggregatorConfig,
    AggregatorFailure,
    AggregatorTarget,
    ExternalJobCandidate,
    TextHttpClient,
)
from .discovery import DiscoveryRunSummary, DiscoverySeed
from .source_resolution import (
    AuthoritativeHandoff,
    CandidateResolution,
    append_candidate_provenance,
    resolution_identity,
    resolve_candidate,
)
from .sources import JsonHttpClient, SourceFailure, SourceJob


@dataclass(slots=True)
class AggregatorPreparation:
    seeds: list[DiscoverySeed] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)
    candidates: int = 0
    resolved: int = 0
    unresolved: int = 0
    duplicates: int = 0
    source_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_dir: str = ""

    def apply_to(self, summary: DiscoveryRunSummary) -> DiscoveryRunSummary:
        summary.aggregator_candidates = self.candidates
        summary.aggregator_resolved = self.resolved
        summary.aggregator_unresolved = self.unresolved
        summary.aggregator_duplicates = self.duplicates
        summary.aggregator_source_status = self.source_status
        summary.aggregator_output = self.output_dir
        summary.selected_sources = sorted(
            set(summary.selected_sources) | set(self.source_status)
        )
        return summary


class AggregatorDiscoveryCoordinator:
    """Prepare authoritative seeds while preserving all lead-resolution audit data."""

    def __init__(
        self,
        *,
        config: AggregatorConfig,
        output_dir: str | Path,
        text_client: TextHttpClient,
        json_client: JsonHttpClient,
        aggregator_adapter: AggregatorAdapter | None = None,
        handoff: AuthoritativeHandoff | None = None,
    ):
        self.config = config
        self.output_dir = Path(output_dir)
        self.adapter = aggregator_adapter or AggregatorAdapter(text_client)
        self.handoff = handoff or AuthoritativeHandoff(json_client)

    def prepare(
        self,
        *,
        sources: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> AggregatorPreparation:
        selected = {
            str(item).strip().casefold() for item in (sources or []) if str(item).strip()
        }
        unsupported = sorted(selected - set(AGGREGATOR_SOURCES))
        if unsupported:
            raise ValueError("Unsupported aggregator sources: " + ", ".join(unsupported))
        if limit is not None and limit < 1:
            raise ValueError("Aggregator limit must be positive.")
        self._prepare_output()
        result = AggregatorPreparation(output_dir=str(self.output_dir))
        resolved_records: list[dict[str, Any]] = []
        unresolved_records: list[dict[str, Any]] = []
        candidate_records: list[dict[str, Any]] = []
        provenance_records: list[dict[str, Any]] = []
        error_records: list[dict[str, Any]] = []
        jobs_by_resolution: dict[str, SourceJob] = {}
        remaining = limit

        targets = [
            target for target in self.config.targets
            if target.enabled and (not selected or target.source in selected)
        ]
        per_source: dict[str, list[dict[str, Any]]] = {}
        for target_index, target in enumerate(targets):
            if remaining is not None and remaining <= 0:
                break
            maximum = min(target.max_results, remaining) if remaining is not None else None
            try:
                batch = self.adapter.fetch(target, limit=maximum)
            except Exception as exc:  # isolate one aggregator target from all others
                failure = AggregatorFailure(
                    source=target.source,
                    status="temporarily_failed",
                    url=target.search_url or str(target.input_file or ""),
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
                per_source.setdefault(target.source, []).append(
                    self._source_attempt(target_index, target, "temporarily_failed", 0, 0, [failure])
                )
                self._append_failure(result, error_records, failure)
                continue

            per_source.setdefault(target.source, []).append(
                self._source_attempt(
                    target_index,
                    target,
                    batch.status,
                    len(batch.candidates),
                    batch.requests_made,
                    batch.errors,
                    batch.access_method,
                )
            )
            for failure in batch.errors:
                self._append_failure(result, error_records, failure)
            for candidate in batch.candidates:
                if remaining is not None and remaining <= 0:
                    break
                result.candidates += 1
                if remaining is not None:
                    remaining -= 1
                self._write_candidate(candidate)
                candidate_records.append(
                    {"discovered_at": _utc_now(), **candidate.to_dict()}
                )
                resolution = resolve_candidate(candidate)
                record = {
                    "candidate": candidate.to_dict(),
                    "resolution": resolution.to_dict(),
                }
                if resolution.status != "resolved_supported_ats" or resolution.ats is None:
                    result.unresolved += 1
                    unresolved_records.append(record)
                    self._write_resolution("unresolved", candidate, record)
                    continue

                self._apply_filter_target(resolution, target)
                identity = resolution_identity(resolution)
                existing = jobs_by_resolution.get(identity)
                if existing is not None:
                    append_candidate_provenance(existing, candidate, resolution)
                    result.duplicates += 1
                    result.resolved += 1
                    resolution.handoff_status = "reused_authoritative_job"
                    resolution.reason += " Existing authoritative job was reused; provenance was merged."
                    record["resolution"] = resolution.to_dict()
                    record["authoritative_job"] = existing.to_dict()
                    resolved_records.append(record)
                    provenance_records.extend(existing.metadata.get("_discovery_provenance", []))
                    self._write_resolution("resolved", candidate, record)
                    continue

                handoff = self.handoff.fetch(candidate, resolution)
                resolution.handoff_errors = list(handoff.errors)
                for failure in handoff.errors:
                    result.failures.append(failure)
                    error_records.append({"stage": "authoritative_handoff", **failure.to_dict()})
                if handoff.job is None:
                    resolution.status = "blocked_or_unavailable"
                    resolution.handoff_status = "failed"
                    resolution.reason = handoff.reason
                    result.unresolved += 1
                    record["resolution"] = resolution.to_dict()
                    unresolved_records.append(record)
                    self._write_resolution("unresolved", candidate, record)
                    continue

                # The production handoff already attaches this record; merging here
                # keeps the coordinator contract explicit and idempotent for adapters.
                append_candidate_provenance(handoff.job, candidate, resolution)
                handoff.job.metadata["authoritative_source"] = resolution.authoritative_source
                resolution.handoff_status = "success"
                resolution.reason = handoff.reason
                record["resolution"] = resolution.to_dict()
                record["authoritative_job"] = handoff.job.to_dict()
                seed = DiscoverySeed(job=handoff.job, target=resolution.ats.target)
                jobs_by_resolution[identity] = handoff.job
                result.seeds.append(seed)
                result.resolved += 1
                resolved_records.append(record)
                provenance_records.extend(handoff.job.metadata.get("_discovery_provenance", []))
                self._write_resolution("resolved", candidate, record)

        result.source_status = {
            source: self._summarize_source(source, attempts)
            for source, attempts in sorted(per_source.items())
        }
        for source in sorted(selected - set(result.source_status)):
            result.source_status[source] = {
                "source": source,
                "status": "not_configured",
                "candidate_count": 0,
                "requests_made": 0,
                "attempts": [],
            }
        _write_json(self.output_dir / "raw_candidates" / "current_queue.json", candidate_records)
        _write_json(self.output_dir / "resolved" / "current_queue.json", resolved_records)
        _write_json(self.output_dir / "unresolved" / "current_queue.json", unresolved_records)
        _write_json(self.output_dir / "errors" / "aggregator_current_queue.json", error_records)
        _write_json(
            self.output_dir / "provenance" / "current.json",
            _unique_records(provenance_records),
        )
        _write_json(self.output_dir / "aggregator_source_status.json", result.source_status)
        return result

    @staticmethod
    def _apply_filter_target(
        resolution: CandidateResolution, aggregator_target: AggregatorTarget
    ) -> None:
        target = resolution.ats.target
        target.search_keywords = list(aggregator_target.search_keywords)
        target.location_filter = list(aggregator_target.location_filter)
        target.employment_types = list(aggregator_target.employment_types)
        target.job_families = list(aggregator_target.job_families)
        target.posted_within_days = aggregator_target.posted_within_days

    def _append_failure(
        self,
        result: AggregatorPreparation,
        records: list[dict[str, Any]],
        failure: AggregatorFailure,
    ) -> None:
        source_failure = SourceFailure(
            source=failure.source,
            url=failure.url,
            error_type=failure.error_type,
            reason=f"{failure.status}: {failure.reason}",
        )
        result.failures.append(source_failure)
        records.append({"stage": "aggregator_access", **failure.to_dict()})

    @staticmethod
    def _source_attempt(
        index: int,
        target: AggregatorTarget,
        status: str,
        candidates: int,
        requests: int,
        errors: Sequence[AggregatorFailure],
        access_method: str = "",
    ) -> dict[str, Any]:
        return {
            "target_index": index,
            "status": status,
            "access_method": access_method,
            "url": target.search_url or str(target.input_file or ""),
            "candidates": candidates,
            "requests_made": requests,
            "errors": [item.to_dict() for item in errors],
        }

    @staticmethod
    def _summarize_source(source: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = [str(item.get("status") or "") for item in attempts]
        if "success" in statuses:
            status = "success"
        elif "blocked_or_unavailable" in statuses:
            status = "blocked_or_unavailable"
        elif "temporarily_failed" in statuses:
            status = "temporarily_failed"
        elif "invalid_config" in statuses:
            status = "invalid_config"
        else:
            status = "empty"
        return {
            "source": source,
            "status": status,
            "candidate_count": sum(int(item.get("candidates") or 0) for item in attempts),
            "requests_made": sum(int(item.get("requests_made") or 0) for item in attempts),
            "attempts": attempts,
        }

    def _prepare_output(self) -> None:
        for name in (
            "raw_candidates", "resolved", "unresolved", "recommended", "irrelevant",
            "errors", "state", "provenance",
        ):
            (self.output_dir / name).mkdir(parents=True, exist_ok=True)

    def _write_candidate(self, candidate: ExternalJobCandidate) -> None:
        _write_json(
            self.output_dir / "raw_candidates" / f"{_safe(candidate.identity)}.json",
            {"discovered_at": _utc_now(), **candidate.to_dict()},
        )

    def _write_resolution(
        self, directory: str, candidate: ExternalJobCandidate, record: Mapping[str, Any]
    ) -> None:
        _write_json(
            self.output_dir / directory / f"{_safe(candidate.identity)}.json",
            dict(record),
        )

def _unique_records(values: Sequence[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        key = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    return records


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._") or "candidate"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
