from __future__ import annotations

import json
import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from jobhuntbot.answer_bank import (
    AnswerBankError,
    AnswerResolver,
    ApplicationQuestion,
    load_answer_bank,
    normalize_question,
)
from jobhuntbot.application_profile import ApplicationProfileError, load_application_profile
from jobhuntbot.application_queue import (
    QUEUE_STATUSES,
    ApplicationHistoryStore,
    ApplicationPackageBuilder,
    ApplicationQueueError,
    ApplicationQueueStore,
)
from jobhuntbot.cli import main


def fact(value=None, status="unknown", source="", notes=""):
    return {
        "value": value,
        "status": status,
        "source": source,
        "last_verified_at": "2026-08-11" if status == "confirmed" else "",
        "notes": notes,
    }


def profile_value(required=None):
    return {
        "schema_version": 1,
        "identity": {
            "legal_first_name": fact("Ada", "confirmed", "user"),
            "legal_last_name": fact("Example", "confirmed", "user"),
            "email": fact("ada@example.invalid", "confirmed", "user"),
        },
        "work_authorization": {
            "authorized_to_work_us": fact(True, "confirmed", "user"),
            "requires_sponsorship_now": fact(False, "confirmed", "user"),
            "requires_sponsorship_future": fact(False, "confirmed", "user"),
            "us_citizen": fact(),
            "permanent_resident": fact(True, "confirmed", "user"),
            "security_clearance": fact(),
        },
        "education": [],
        "employment_history": [],
        "links": {},
        "job_preferences": {
            "salary_response_policy": fact({"strategy": "manual", "amount": None}, "confirmed", "user"),
            "location_policy": fact(
                {
                    "local_acceptable": ["Example City"],
                    "relocation_allowed": ["Example State"],
                    "relocation_requires_confirmation": True,
                },
                "confirmed",
                "user",
            ),
        },
        "application_policies": {
            "years_of_experience": fact(),
            "years_python_experience": fact(),
            "driver_license": fact(),
        },
        "demographics_policy": {
            "default_response": fact("prefer_not_to_answer", "confirmed", "user"),
            "allowed_autofill": fact(False, "confirmed", "user"),
        },
        "conflicts": [],
        "required_for_ready": required or [],
    }


def bank_entry(canonical_id, path="", **overrides):
    value = {
        "canonical_id": canonical_id,
        "category": "test",
        "answer_type": "KNOWN_FACT",
        "value": None,
        "status": "confirmed",
        "confidence": "HIGH",
        "provenance": ["private_profile"],
        "reusable": True,
        "job_dependent": False,
        "requires_user_confirmation": False,
        "allowed_autofill": False,
        "safety_class": "STATIC_CONFIRMED",
        "profile_fact_path": path,
        "notes": "",
    }
    value.update(overrides)
    return value


def pipeline_result(decision="APPLY", job_id="job-1", title="Analyst", resume_path="resume.pdf"):
    return {
        "job": {
            "job_id": job_id,
            "company": "Example Co",
            "title": title,
            "source": "greenhouse",
            "source_url": "https://example.invalid/job",
            "apply_url": "https://example.invalid/apply",
            "location": "Example City",
            "salary": {"minimum": 80000, "maximum": 100000},
            "skills_required": ["sql"],
        },
        "score": {"recommendation": decision, "overall_score": 80.0},
        "resume": {"resume_id": "data_bi", "file_path": resume_path, "selected": True},
    }


class ApplicationPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "my-materials"
        self.application_root = self.root / "application"
        self.application_root.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, name, value):
        path = self.application_root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def load_profile(self, value=None):
        return load_application_profile(self.write_json("profile.json", value or profile_value()))

    def load_bank(self, entries):
        return load_answer_bank(self.write_json("bank.json", {"schema_version": 1, "entries": entries}))

    def test_confirmed_fact_requires_provenance(self):
        value = profile_value()
        value["identity"]["legal_first_name"] = fact("Ada", "confirmed", "")
        with self.assertRaises(ApplicationProfileError):
            self.load_profile(value)

    def test_invalid_fact_status_is_rejected(self):
        value = profile_value()
        value["identity"]["legal_first_name"]["status"] = "probably"
        with self.assertRaises(ApplicationProfileError):
            self.load_profile(value)

    def test_confirmed_false_is_a_usable_fact(self):
        profile = self.load_profile()
        self.assertTrue(profile.get_fact("work_authorization.requires_sponsorship_now").usable)
        self.assertIs(profile.get_fact("work_authorization.requires_sponsorship_now").value, False)

    def test_question_normalization_keeps_authorization_concepts_separate(self):
        auth = normalize_question(ApplicationQuestion("a", "Are you legally authorized to work in the United States?"))
        citizen = normalize_question(ApplicationQuestion("b", "Are you a U.S. citizen?"))
        resident = normalize_question(ApplicationQuestion("c", "Are you a permanent resident?"))
        self.assertEqual((auth, citizen, resident), ("work_authorization_us", "us_citizen", "permanent_resident"))

    def test_known_authorization_is_resolved_from_confirmed_profile(self):
        bank = self.load_bank([bank_entry("work_authorization_us", "work_authorization.authorized_to_work_us")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Authorized to work in the US?", "boolean")], {}
        )
        self.assertEqual(result.answers[0].value, True)
        self.assertEqual(result.unresolved, [])

    def test_unknown_citizenship_is_not_derived_from_authorization(self):
        bank = self.load_bank([bank_entry("us_citizen", "work_authorization.us_citizen", answer_type="NEVER_GUESS", status="unknown", provenance=[], safety_class="NEVER_GUESS")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Are you a US citizen?", "boolean")], {}
        )
        self.assertEqual(result.answers, [])
        self.assertEqual(result.unresolved[0].reason_code, "FACT_NOT_CONFIRMED")

    def test_source_conflict_stays_unresolved(self):
        value = profile_value()
        value["conflicts"] = [{"fact_path": "identity.email", "sources": ["a", "b"], "reason": "different", "status": "unresolved"}]
        bank = self.load_bank([bank_entry("contact_email", "identity.email")])
        question = ApplicationQuestion("q", "Email", canonical_id="contact_email")
        result = AnswerResolver(self.load_profile(value), bank).resolve([question], {})
        self.assertEqual(result.unresolved[0].reason_code, "SOURCE_CONFLICT")

    def test_generic_years_unknown_remains_unresolved(self):
        bank = self.load_bank([bank_entry("years_of_experience", "application_policies.years_of_experience", status="unknown", provenance=[], answer_type="UNKNOWN", safety_class="NEVER_GUESS")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Total years of professional experience", "number")], {}
        )
        self.assertEqual(result.unresolved[0].canonical_id, "years_of_experience")

    def test_domain_years_do_not_fall_back_to_generic_years(self):
        bank = self.load_bank([bank_entry("years_python_experience", "application_policies.years_python_experience", status="unknown", provenance=[], answer_type="UNKNOWN", safety_class="NEVER_GUESS")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "How many years of Python experience?", "number")], {}
        )
        self.assertEqual(result.unresolved[0].canonical_id, "years_python_experience")

    def test_manual_salary_policy_is_unresolved(self):
        bank = self.load_bank([bank_entry("desired_salary", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT", status="unknown", provenance=[])])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Desired salary", "number")], {"salary": {"minimum": 80, "maximum": 100}}
        )
        self.assertEqual(result.unresolved[0].reason_code, "SALARY_USER_INPUT")

    def test_posted_range_midpoint_strategy_is_deterministic(self):
        value = profile_value()
        value["job_preferences"]["salary_response_policy"] = fact({"strategy": "posted_range_midpoint"}, "confirmed", "user")
        bank = self.load_bank([bank_entry("desired_salary", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT")])
        result = AnswerResolver(self.load_profile(value), bank).resolve(
            [ApplicationQuestion("q", "Compensation expectation", "number")], {"salary": {"minimum": 80, "maximum": 100}}
        )
        self.assertEqual(result.answers[0].value, 90.0)

    def test_explicit_salary_outside_posted_range_is_conflict(self):
        value = profile_value()
        value["job_preferences"]["salary_response_policy"] = fact({"strategy": "explicit_amount", "amount": 120}, "confirmed", "user")
        bank = self.load_bank([bank_entry("desired_salary", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT")])
        result = AnswerResolver(self.load_profile(value), bank).resolve(
            [ApplicationQuestion("q", "Salary", "number")], {"salary": {"minimum": 80, "maximum": 100}}
        )
        self.assertEqual(result.unresolved[0].reason_code, "SALARY_CONFLICT")

    def test_local_relocation_answer_uses_private_policy(self):
        bank = self.load_bank([bank_entry("willing_to_relocate", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Are you willing to relocate?", "boolean")], {"location": "Example City"}
        )
        self.assertEqual(result.answers[0].value, True)

    def test_conditional_relocation_market_requires_confirmation(self):
        bank = self.load_bank([bank_entry("willing_to_relocate", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Relocation", "boolean")], {"location": "Example State"}
        )
        self.assertEqual(result.unresolved[0].reason_code, "RELOCATION_CONFIRMATION")

    def test_optional_demographic_uses_prefer_not_policy_only_when_optional(self):
        bank = self.load_bank([bank_entry("gender", answer_type="OPTIONAL_PREFER_NOT", status="unknown", provenance=[], reusable=False, safety_class="OPTIONAL_PREFER_NOT_TO_ANSWER")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Gender", "select", required=False)], {}
        )
        self.assertEqual(result.answers[0].value, "prefer_not_to_answer")
        self.assertFalse(result.answers[0].allowed_autofill)

    def test_required_demographic_stays_manual(self):
        bank = self.load_bank([bank_entry("gender", answer_type="OPTIONAL_PREFER_NOT", status="unknown", provenance=[], reusable=False, safety_class="OPTIONAL_PREFER_NOT_TO_ANSWER")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Gender", "select", required=True)], {}
        )
        self.assertEqual(result.unresolved[0].reason_code, "OPTIONAL_MANUAL")

    def test_unknown_question_is_first_class_unresolved(self):
        result = AnswerResolver(self.load_profile(), self.load_bank([])).resolve(
            [ApplicationQuestion("q", "Name your favorite constellation")], {}
        )
        self.assertEqual(result.unresolved[0].reason_code, "NO_ANSWER_BANK_ENTRY")

    def test_confirm_before_use_stays_unresolved_even_with_candidate_value(self):
        bank = self.load_bank([bank_entry("contact_email", "identity.email", safety_class="CONFIRM_BEFORE_USE", requires_user_confirmation=True)])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Email", canonical_id="contact_email")], {}
        )
        self.assertEqual(result.unresolved[0].reason_code, "CONFIRM_BEFORE_USE")

    def test_clearance_is_not_inferred_from_permanent_residence(self):
        bank = self.load_bank([bank_entry("security_clearance", "work_authorization.security_clearance", answer_type="NEVER_GUESS", status="unknown", provenance=[], safety_class="NEVER_GUESS")])
        result = AnswerResolver(self.load_profile(), bank).resolve(
            [ApplicationQuestion("q", "Do you hold a security clearance?", "boolean")], {}
        )
        self.assertEqual(result.answers, [])
        self.assertEqual(result.unresolved[0].canonical_id, "security_clearance")

    def test_midpoint_salary_strategy_needs_a_posted_range(self):
        value = profile_value()
        value["job_preferences"]["salary_response_policy"] = fact({"strategy": "posted_range_midpoint"}, "confirmed", "user")
        bank = self.load_bank([bank_entry("desired_salary", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT")])
        result = AnswerResolver(self.load_profile(value), bank).resolve(
            [ApplicationQuestion("q", "Salary expectation", "number")], {"salary": None}
        )
        self.assertEqual(result.unresolved[0].reason_code, "SALARY_USER_INPUT")

    def test_unsafe_autofill_configuration_is_rejected(self):
        entry = bank_entry("desired_salary", allowed_autofill=True, safety_class="JOB_DEPENDENT", job_dependent=True)
        with self.assertRaises(AnswerBankError):
            self.load_bank([entry])

    def test_apply_imports_to_ready_to_prepare(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result()], source_path="private.json")
        self.assertEqual(report.added, 1)
        self.assertEqual(store.load()["applications"][0]["application_status"], "READY_TO_PREPARE")

    def test_review_is_excluded_by_default(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result("REVIEW")], source_path="private.json")
        self.assertEqual((report.added, report.review_excluded), (0, 1))

    def test_review_requires_explicit_promotion(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result("REVIEW")], source_path="private.json", promote_review_job_ids={"job-1"})
        record = store.load()["applications"][0]
        self.assertEqual(report.promoted_review, 1)
        self.assertEqual(record["decision"], "REVIEW")
        self.assertTrue(record["promotion"]["promoted"])

    def test_skip_is_always_excluded(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result("SKIP")], source_path="private.json", promote_review_job_ids={"job-1"})
        self.assertEqual((report.added, report.skip_excluded), (0, 1))

    def test_stable_job_id_deduplicates(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        store.import_candidates([pipeline_result()], source_path="one.json")
        report = store.import_candidates([pipeline_result()], source_path="two.json")
        self.assertEqual(report.duplicate, 1)
        self.assertEqual(len(store.load()["applications"]), 1)

    def test_material_change_does_not_overwrite_existing_snapshot(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        store.import_candidates([pipeline_result(title="Original")], source_path="one.json")
        report = store.import_candidates([pipeline_result(title="Changed")], source_path="two.json")
        record = store.load()["applications"][0]
        self.assertEqual(report.changed, 1)
        self.assertEqual(record["job_snapshot"]["title"], "Original")
        self.assertEqual(record["pending_update"]["job_snapshot"]["title"], "Changed")
        self.assertEqual(record["application_status"], "NEEDS_REVIEW")

    def test_queue_output_outside_my_materials_is_rejected(self):
        with self.assertRaises(ApplicationQueueError):
            ApplicationQueueStore(Path(self.temp.name) / "queue.json")

    def test_missing_resume_blocks_package(self):
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result(resume_path="missing.pdf")], source_path="private.json")
        package = ApplicationPackageBuilder(queue_store=store, application_root=self.application_root, profile=self.load_profile(), answer_bank=self.load_bank([])).build(report.application_ids[0], [])
        self.assertEqual(package.package_readiness, "BLOCKED")

    def test_unconfirmed_required_fact_needs_user_input(self):
        value = profile_value(required=["identity.phone"])
        value["identity"]["phone"] = fact()
        resume = self.root / "resume.pdf"
        resume.write_bytes(b"%PDF-test")
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result(resume_path=str(resume))], source_path="private.json")
        package = ApplicationPackageBuilder(queue_store=store, application_root=self.application_root, profile=self.load_profile(value), answer_bank=self.load_bank([])).build(report.application_ids[0], [])
        self.assertEqual(package.package_readiness, "NEEDS_USER_INPUT")
        self.assertEqual(package.unresolved_questions[0]["reason_code"], "REQUIRED_CANDIDATE_FACT")

    def test_verified_resume_is_used_without_substitution(self):
        resume = self.root / "selected.pdf"
        resume.write_bytes(b"%PDF-test")
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result(resume_path=str(resume))], source_path="private.json")
        package = ApplicationPackageBuilder(queue_store=store, application_root=self.application_root, profile=self.load_profile(), answer_bank=self.load_bank([])).build(report.application_ids[0], [])
        self.assertEqual(package.package_readiness, "READY")
        self.assertEqual(Path(package.selected_resume["path"]), resume.resolve())
        self.assertFalse(package.selected_resume["substitution_allowed"])

    def test_optional_unresolved_question_does_not_block_ready_package(self):
        resume = self.root / "selected.pdf"
        resume.write_bytes(b"%PDF-test")
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result(resume_path=str(resume))], source_path="private.json")
        package = ApplicationPackageBuilder(queue_store=store, application_root=self.application_root, profile=self.load_profile(), answer_bank=self.load_bank([])).build(
            report.application_ids[0], [ApplicationQuestion("optional", "Favorite constellation", required=False)]
        )
        self.assertEqual(package.package_readiness, "READY")
        self.assertEqual(len(package.unresolved_questions), 1)

    def test_package_exposes_job_range_strategy_and_confirmation(self):
        resume = self.root / "selected.pdf"
        resume.write_bytes(b"%PDF-test")
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([pipeline_result(resume_path=str(resume))], source_path="private.json")
        bank = self.load_bank([bank_entry("desired_salary", job_dependent=True, reusable=False, answer_type="JOB_DEPENDENT", safety_class="JOB_DEPENDENT", status="unknown", provenance=[])])
        package = ApplicationPackageBuilder(queue_store=store, application_root=self.application_root, profile=self.load_profile(), answer_bank=bank).build(
            report.application_ids[0], [ApplicationQuestion("salary", "Desired salary", "number")]
        )
        salary = package.job_dependent_answers["salary"]
        self.assertEqual(salary["salary_range_in_job"]["minimum"], 80000)
        self.assertEqual(salary["candidate_strategy"]["strategy"], "manual")
        self.assertTrue(salary["requires_confirmation"])

    def test_tracked_templates_use_only_fictional_placeholders(self):
        repository = Path(__file__).parents[1]
        content = "\n".join(
            (repository / "templates" / name).read_text(encoding="utf-8")
            for name in ("candidate_application_profile.template.json", "answer_bank.template.json")
        )
        self.assertNotIn("@gmail.com", content)
        self.assertIsNone(re.search(r"\b\d{3}[- ]\d{3}[- ]\d{4}\b", content))
        self.assertNotIn('"status": "confirmed"', content)
        self.assertIn("Example University", content)

    def test_history_rejects_sensitive_value_duplication(self):
        history = ApplicationHistoryStore(self.application_root / "history" / "events.jsonl")
        with self.assertRaises(ApplicationQueueError):
            history.append("answer_resolved", "application-1", value="secret")

    def test_phase3_json_cli_escapes_console_incompatible_job_text(self):
        resume = self.root / "selected.pdf"
        resume.write_bytes(b"%PDF-test")
        candidate = pipeline_result(resume_path=str(resume))
        candidate["job"]["job_description"] = "replacement character: \ufffd"
        store = ApplicationQueueStore(self.application_root / "queue.json")
        report = store.import_candidates([candidate], source_path="private.json")
        profile_path = self.write_json("profile.json", profile_value())
        bank_path = self.write_json("bank.json", {"schema_version": 1, "entries": []})
        questions_path = self.write_json("questions.json", {"questions": []})
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main([
                "application-package-build",
                "--application-id", report.application_ids[0],
                "--application-root", str(self.application_root),
                "--queue", str(self.application_root / "queue.json"),
                "--application-profile", str(profile_path),
                "--answer-bank", str(bank_path),
                "--questions", str(questions_path),
                "--json",
            ])
        self.assertEqual(exit_code, 0)
        self.assertIn("\\ufffd", output.getvalue())
        self.assertNotIn("\ufffd", output.getvalue())

    def test_phase_has_no_submitted_queue_state(self):
        self.assertNotIn("SUBMITTED", QUEUE_STATUSES)


if __name__ == "__main__":
    unittest.main()
