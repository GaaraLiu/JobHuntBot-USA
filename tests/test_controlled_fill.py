from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from jobhuntbot.application_forms.models import (
    ApplicationField,
    ApplicationForm,
    ApplicationMappingPlan,
    FieldMappingPlan,
    FormSection,
)
from jobhuntbot.browser_forms import BrowserFieldSnapshot, BrowserRenderedForm
from jobhuntbot.controlled_fill import (
    ControlledFillPlanner,
    ControlledFillPolicy,
    ControlledFillResult,
    ControlledFillService,
    MutationFirewall,
    PlaywrightControlledFillBrowser,
    sanitized_fill_log,
)
from jobhuntbot.cli import main
from jobhuntbot.controlled_fill.playwright_fill import _SUBMISSION_FIREWALL_SCRIPT


def confirmed(value):
    return {"value": value, "status": "confirmed", "source": "fictional test"}


SOURCE_BY_CONCEPT = {
    "legal_first_name": "identity.legal_first_name",
    "legal_middle_name": "identity.legal_middle_name",
    "legal_last_name": "identity.legal_last_name",
    "legal_full_name": "compose(identity.legal_first_name, identity.legal_middle_name?, identity.legal_last_name)",
    "contact_email": "identity.email",
    "contact_phone": "identity.phone",
    "address.street": "identity.current_address",
    "address.city": "identity.current_city",
    "address.state": "identity.current_state",
    "address.postal_code": "identity.postal_code",
    "address.country": "identity.current_country",
    "linkedin": "links.linkedin",
    "github": "links.github",
    "portfolio": "links.portfolio",
    "work_authorization_us": "work_authorization.authorized_to_work_us",
    "sponsorship_now": "work_authorization.requires_sponsorship_now",
    "sponsorship_future": "work_authorization.requires_sponsorship_future",
    "driver_license": "application_policies.driver_license",
}


class FakeFillBackend:
    def __init__(self):
        self.calls = 0

    def fill(self, url, plan, form, rendered, policy):
        self.calls += 1
        return ControlledFillResult(
            application_id=plan.application_id,
            form_fingerprint=plan.form_fingerprint,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            total_fields=plan.total_fields,
            attempted_fields=plan.fill_fields,
            filled_fields=plan.fill_fields,
            skipped_fields=plan.skipped_fields,
            manual_only_fields=plan.manual_only_fields,
            unresolved_fields=plan.unresolved_fields,
            failed_fields=0,
            blocked_mutation_requests=[],
            captcha_detected=False,
            login_required=False,
            submission_attempted=False,
            submission_completed=False,
            overall_status="FILLED_SAFE_FIELDS",
        )


class ControlledFillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.resume = self.root / "fictional_resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4\n% fictional test\n")
        self.package = {
            "application_id": "application_fictional",
            "selected_resume": {
                "resume_id": "fictional_route",
                "path": str(self.resume),
                "verified_exists": True,
                "substitution_allowed": False,
            },
            "candidate_facts": {
                "identity.legal_first_name": confirmed("Avery"),
                "identity.legal_middle_name": confirmed("NONE"),
                "identity.legal_last_name": confirmed("Example"),
                "identity.email": confirmed("avery@example.invalid"),
                "identity.phone": confirmed("+1 555-0100"),
                "identity.current_address": confirmed("1 Fictional Way"),
                "identity.current_city": confirmed("Example City"),
                "identity.current_state": confirmed("EX"),
                "identity.postal_code": confirmed("00000"),
                "identity.current_country": confirmed("United States"),
                "links.linkedin": confirmed("https://example.invalid/linkedin"),
                "links.github": confirmed("https://example.invalid/github"),
                "links.portfolio": confirmed("https://example.invalid/portfolio"),
                "work_authorization.authorized_to_work_us": confirmed(True),
                "work_authorization.requires_sponsorship_now": confirmed(False),
                "work_authorization.requires_sponsorship_future": confirmed(False),
                "application_policies.driver_license": confirmed(True),
                "education[0].institution": confirmed("Fictional University"),
                "employment[0].employer": confirmed("Fictional Employer"),
                "application_policies.years_python_experience": confirmed(3),
            },
            "prepared_answers": [],
            "job_dependent_answers": {},
        }

    def tearDown(self):
        self.temp.cleanup()

    def artifacts(self, specs, *, anti_bot=False, login=False, mapping_fingerprint=None):
        form_fields = []
        browser_fields = []
        mappings = []
        for index, spec in enumerate(specs, start=1):
            canonical = spec["canonical"]
            field_id = spec.get("field_id", f"field_{index}")
            label = spec.get("label", canonical.replace("_", " "))
            field_type = spec.get("type", "text")
            required = spec.get("required", True)
            options = list(spec.get("options", []))
            locator = spec.get("locator", f"#{field_id}")
            form_fields.append(ApplicationField(
                field_id=field_id,
                section="application",
                label=label,
                normalized_label=label.casefold(),
                field_type=field_type,
                required=required,
                options=options,
                source_path=locator,
            ))
            browser_fields.append(BrowserFieldSnapshot(
                field_id=field_id,
                section_id="application",
                section_label="Application",
                label=spec.get("browser_label", label),
                dom_type=spec.get("browser_type", field_type),
                required=spec.get("browser_required", required),
                options=options,
                visible=spec.get("visible", True),
                disabled=spec.get("disabled", False),
                locator=spec.get("browser_locator", locator),
            ))
            mappings.append(FieldMappingPlan(
                field_id=field_id,
                label=label,
                required=required,
                canonical_question_id=canonical,
                mapping_confidence=spec.get("confidence", "HIGH"),
                safety_class=spec.get("safety", "STATIC_CONFIRMED"),
                answer_status=spec.get("answer_status", "confirmed"),
                action=spec.get("mapping_action", "AUTO_READY_FUTURE"),
                reason="fictional test mapping",
                source_reference=spec.get("source", SOURCE_BY_CONCEPT.get(canonical, "")),
            ))
        form = ApplicationForm(
            source="fictional_fixture",
            ats="lever",
            application_url="https://example.invalid/jobs/apply",
            job_id="fictional_job",
            company="Fictional Company",
            title="Fictional Role",
            detected_at="2026-01-01T00:00:00+00:00",
            sections=[FormSection("application", "Application", form_fields)],
        )
        form.compute_fingerprint()
        mapping = ApplicationMappingPlan(
            application_id="application_fictional",
            job_id="fictional_job",
            form_fingerprint=mapping_fingerprint or form.fingerprint,
            ats="lever",
            generated_at="2026-01-01T00:00:00+00:00",
            fields=mappings,
            total_fields=len(mappings),
            mapped_fields=len(mappings),
            required_fields=sum(item.required for item in mappings),
            required_unresolved=0,
            manual_only=0,
            potential_future_autofill=len(mappings),
            user_confirmation=0,
            optional_skip=0,
            package_readiness="READY",
        )
        status = "BLOCKED_BY_ANTI_BOT" if anti_bot else "LOGIN_REQUIRED" if login else "DETECTED"
        rendered = BrowserRenderedForm(
            requested_url=form.application_url,
            final_url=form.application_url,
            page_title=form.title,
            ats_hint="lever",
            status=status,
            fields=browser_fields,
            anti_bot=anti_bot,
            login_wall=login,
        )
        return form, mapping, rendered

    def plan(self, specs, *, package=None, **artifact_options):
        form, mapping, rendered = self.artifacts(specs, **artifact_options)
        return ControlledFillPlanner().build(package or self.package, mapping, form, rendered), form, rendered

    def action(self, canonical, **spec):
        plan, _, _ = self.plan([{"canonical": canonical, **spec}])
        return plan.fields[0]

    def test_identity_contact_and_full_name_are_fillable_from_confirmed_package_facts(self):
        for canonical in ("legal_first_name", "legal_last_name", "legal_full_name", "contact_email", "contact_phone"):
            with self.subTest(canonical=canonical):
                item = self.action(canonical, type="email" if canonical == "contact_email" else "phone" if canonical == "contact_phone" else "text")
                self.assertEqual(item.action, "FILL")
        full = self.action("legal_full_name")
        self.assertEqual(full.resolved_value, "Avery Example")

    def test_address_and_links_are_allowlisted_when_confirmed(self):
        for canonical in ("address.street", "address.city", "address.state", "address.postal_code", "address.country", "linkedin", "github", "portfolio"):
            with self.subTest(canonical=canonical):
                self.assertEqual(self.action(canonical).action, "FILL")

    def test_work_authorization_sponsorship_and_driver_license_match_exact_options(self):
        for canonical, expected in (
            ("work_authorization_us", "Yes"),
            ("sponsorship_now", "No"),
            ("sponsorship_future", "No"),
            ("driver_license", "Yes"),
        ):
            with self.subTest(canonical=canonical):
                item = self.action(canonical, type="radio", options=["Yes", "No"])
                self.assertEqual(item.action, "FILL")
                self.assertEqual(item.intended_option, expected)

    def test_confirmed_repeatable_and_domain_year_facts_can_fill_only_with_approved_mapping(self):
        specs = [
            {"canonical": "education.institution", "source": "education[0].institution"},
            {"canonical": "employment.employer", "source": "employment[0].employer"},
            {"canonical": "years_python_experience", "source": "application_policies.years_python_experience", "type": "number", "safety": "NEVER_GUESS"},
        ]
        plan, _, _ = self.plan(specs)
        self.assertEqual([item.action for item in plan.fields], ["FILL", "FILL", "FILL"])

    def test_missing_or_unconfirmed_fact_is_never_filled(self):
        package = dict(self.package)
        package["candidate_facts"] = {}
        item = self.plan([{"canonical": "contact_email", "type": "email"}], package=package)[0].fields[0]
        self.assertEqual(item.action, "UNRESOLVED")

    def test_total_experience_remains_wait_for_user(self):
        self.assertEqual(self.action("years_of_experience", type="number", safety="NEVER_GUESS").action, "WAIT_FOR_USER")

    def test_unconfirmed_domain_experience_is_unresolved(self):
        self.assertEqual(self.action("years_power_bi_experience", type="number", safety="NEVER_GUESS", source="application_policies.years_power_bi_experience").action, "UNRESOLVED")

    def test_salary_requires_resolution_and_numeric_salary_requires_explicit_approval(self):
        self.assertEqual(self.action("desired_salary", type="number", safety="JOB_DEPENDENT").action, "WAIT_FOR_USER")
        package = dict(self.package)
        package["job_dependent_answers"] = {"salary": {"proposed_answer": 100000, "requires_confirmation": False, "approved": False}}
        self.assertEqual(self.plan([{"canonical": "desired_salary", "type": "number", "safety": "JOB_DEPENDENT"}], package=package)[0].fields[0].action, "WAIT_FOR_USER")
        package["job_dependent_answers"]["salary"]["approved"] = True
        self.assertEqual(self.plan([{"canonical": "desired_salary", "type": "number", "safety": "JOB_DEPENDENT"}], package=package)[0].fields[0].action, "FILL")

    def test_job_dependent_answer_still_requires_high_confidence_eligible_mapping(self):
        package = dict(self.package)
        package["job_dependent_answers"] = {"salary": {"proposed_answer": 100000, "requires_confirmation": False, "approved": True}}
        item = self.plan([{
            "canonical": "desired_salary", "type": "number", "safety": "JOB_DEPENDENT",
            "mapping_action": "USER_CONFIRMATION", "confidence": "HIGH",
        }], package=package)[0].fields[0]
        self.assertEqual(item.action, "WAIT_FOR_USER")

    def test_confirmed_negotiable_salary_only_fills_text(self):
        package = dict(self.package)
        package["job_dependent_answers"] = {"salary": {"proposed_answer": "Negotiable", "requires_confirmation": False}}
        text_item = self.plan([{"canonical": "desired_salary", "type": "text", "safety": "JOB_DEPENDENT"}], package=package)[0].fields[0]
        number_item = self.plan([{"canonical": "desired_salary", "type": "number", "safety": "JOB_DEPENDENT"}], package=package)[0].fields[0]
        self.assertEqual(text_item.action, "FILL")
        self.assertEqual(number_item.action, "WAIT_FOR_USER")

    def test_resolved_relocation_requires_explicit_approval(self):
        package = dict(self.package)
        package["job_dependent_answers"] = {"relocation": {"proposed_answer": True, "requires_confirmation": False, "approved": True}}
        item = self.plan([{"canonical": "willing_to_relocate", "type": "radio", "options": ["Yes", "No"], "safety": "JOB_DEPENDENT"}], package=package)[0].fields[0]
        self.assertEqual(item.action, "FILL")
        self.assertEqual(item.intended_option, "Yes")

    def test_optional_demographics_are_skipped_and_required_demographics_are_manual(self):
        optional = self.action("gender", required=False, safety="OPTIONAL_PREFER_NOT_TO_ANSWER", mapping_action="OPTIONAL_SKIP", type="select", options=["Prefer not to answer"])
        required = self.action("race_ethnicity", required=True, safety="OPTIONAL_PREFER_NOT_TO_ANSWER", type="select")
        self.assertEqual(optional.action, "SKIP_OPTIONAL")
        self.assertEqual(required.action, "MANUAL_ONLY")

    def test_legal_consent_and_signature_are_always_manual(self):
        for canonical, field_type in (("privacy_acknowledgment", "consent"), ("electronic_signature", "signature")):
            with self.subTest(canonical=canonical):
                self.assertEqual(self.action(canonical, type=field_type, safety="MANUAL_ONLY", mapping_action="MANUAL_ONLY").action, "MANUAL_ONLY")

    def test_resume_is_validated_but_live_upload_is_disabled(self):
        plan, _, _ = self.plan([{"canonical": "resume_upload", "type": "resume", "safety": "MANUAL_ONLY", "mapping_action": "MANUAL_ONLY"}])
        self.assertTrue(plan.intended_resume["verified"])
        self.assertFalse(plan.intended_resume["live_upload_enabled"])
        self.assertEqual(plan.fields[0].action, "MANUAL_ONLY")

    def test_missing_resume_is_a_blocker(self):
        package = dict(self.package)
        package["selected_resume"] = {"resume_id": "fictional", "path": str(self.root / "missing.pdf"), "verified_exists": True}
        plan, _, _ = self.plan([{"canonical": "resume_upload", "type": "resume", "safety": "MANUAL_ONLY", "mapping_action": "MANUAL_ONLY"}], package=package)
        self.assertFalse(plan.intended_resume["verified"])
        self.assertTrue(plan.intended_resume["error"])

    def test_live_file_upload_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            ControlledFillPolicy(execute_fill=True, allow_file_upload=True)

    def test_captcha_stops_before_backend_fill(self):
        plan, form, rendered = self.plan([{"canonical": "contact_email", "type": "email"}], anti_bot=True)
        backend = FakeFillBackend()
        result = ControlledFillService(backend).run(form.application_url, plan, form, rendered, ControlledFillPolicy(execute_fill=True))
        self.assertEqual(result.overall_status, "BLOCKED_BY_CAPTCHA")
        self.assertEqual(backend.calls, 0)

    def test_login_wall_stops_before_backend_fill(self):
        plan, form, rendered = self.plan([{"canonical": "contact_email", "type": "email"}], login=True)
        backend = FakeFillBackend()
        result = ControlledFillService(backend).run(form.application_url, plan, form, rendered, ControlledFillPolicy(execute_fill=True))
        self.assertTrue(result.login_required)
        self.assertEqual(backend.calls, 0)

    def test_stale_form_fingerprint_blocks(self):
        plan, _, _ = self.plan([{"canonical": "contact_email", "type": "email"}], mapping_fingerprint="stale")
        self.assertEqual(plan.overall_status, "FORM_CHANGED_REVIEW_REQUIRED")
        self.assertEqual(plan.fields[0].action, "FILL")

    def test_package_mapping_application_mismatch_blocks_execution(self):
        form, mapping, rendered = self.artifacts([{"canonical": "contact_email", "type": "email"}])
        mapping.application_id = "a_different_application"
        plan = ControlledFillPlanner().build(self.package, mapping, form, rendered)
        self.assertEqual(plan.overall_status, "FORM_CHANGED_REVIEW_REQUIRED")
        backend = FakeFillBackend()
        ControlledFillService(backend).run(
            form.application_url, plan, form, rendered,
            ControlledFillPolicy(execute_fill=True),
        )
        self.assertEqual(backend.calls, 0)

    def test_changed_locator_label_type_required_and_visibility_each_block_field(self):
        variants = (
            {"browser_locator": "#changed"},
            {"browser_label": "Changed label"},
            {"browser_type": "textarea"},
            {"browser_required": False},
            {"visible": False},
            {"disabled": True},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                item = self.action("contact_email", type="email", **variant)
                self.assertEqual(item.action, "BLOCKED")

    def test_preview_is_default_and_does_not_call_backend(self):
        plan, form, rendered = self.plan([{"canonical": "contact_email", "type": "email"}])
        backend = FakeFillBackend()
        result = ControlledFillService(backend).run(form.application_url, plan, form, rendered, ControlledFillPolicy())
        self.assertEqual(result.overall_status, "PREVIEW_READY")
        self.assertEqual(result.attempted_fields, 0)
        self.assertEqual(backend.calls, 0)

    def test_explicit_execute_calls_backend_when_plan_is_ready(self):
        plan, form, rendered = self.plan([{"canonical": "contact_email", "type": "email"}])
        backend = FakeFillBackend()
        result = ControlledFillService(backend).run(form.application_url, plan, form, rendered, ControlledFillPolicy(execute_fill=True))
        self.assertEqual(result.overall_status, "FILLED_SAFE_FIELDS")
        self.assertEqual(backend.calls, 1)

    def test_required_unresolved_blocks_unless_partial_is_explicit(self):
        plan, form, rendered = self.plan([{"canonical": "years_of_experience", "type": "number", "safety": "NEVER_GUESS"}])
        backend = FakeFillBackend()
        ControlledFillService(backend).run(form.application_url, plan, form, rendered, ControlledFillPolicy(execute_fill=True))
        self.assertEqual(backend.calls, 0)
        ControlledFillService(backend).run(form.application_url, plan, form, rendered, ControlledFillPolicy(execute_fill=True, allow_partial_fill=True))
        self.assertEqual(backend.calls, 1)

    def test_package_unresolved_question_prevents_field_fill_and_normal_execution(self):
        package = dict(self.package)
        package["unresolved_questions"] = [{
            "question_id": "field_1", "canonical_id": "contact_email",
            "required": True, "resolved": False,
        }]
        plan, form, rendered = self.plan(
            [{"canonical": "contact_email", "type": "email"}], package=package
        )
        self.assertEqual(plan.fields[0].action, "UNRESOLVED")
        self.assertEqual(plan.overall_status, "NEEDS_USER_INPUT")
        backend = FakeFillBackend()
        ControlledFillService(backend).run(
            form.application_url, plan, form, rendered,
            ControlledFillPolicy(execute_fill=True),
        )
        self.assertEqual(backend.calls, 0)

    def test_custom_combobox_radio_checkbox_and_date_are_plannable(self):
        specs = [
            {"canonical": "work_authorization_us", "type": "select", "options": ["Yes", "No"]},
            {"canonical": "sponsorship_now", "type": "radio", "options": ["Yes", "No"]},
            {"canonical": "driver_license", "type": "checkbox", "options": ["Yes", "No"]},
        ]
        plan, _, _ = self.plan(specs)
        self.assertEqual([item.action for item in plan.fields], ["FILL", "FILL", "FILL"])

    def test_mutation_firewall_blocks_writes_and_sanitizes_urls(self):
        firewall = MutationFirewall()
        self.assertTrue(firewall.allows("GET", "https://example.invalid/apply?q=secret"))
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertFalse(firewall.allows(method, "https://user:secret@example.invalid/save?token=secret"))
        self.assertEqual({item["method"] for item in firewall.blocked}, {"POST", "PUT", "PATCH", "DELETE"})
        self.assertTrue(all("secret" not in item["url"] for item in firewall.blocked))

    def test_plan_and_logs_do_not_serialize_candidate_values(self):
        plan, _, _ = self.plan([{"canonical": "contact_email", "type": "email"}])
        serialized = json.dumps(plan.to_dict())
        self.assertNotIn("avery@example.invalid", serialized)
        result = ControlledFillService()._local_result(plan, "PREVIEW_READY", [])
        self.assertNotIn("avery@example.invalid", json.dumps(sanitized_fill_log(result)))
        self.assertFalse(sanitized_fill_log(result)["candidate_values_logged"])

    def test_result_can_never_represent_submitted(self):
        with self.assertRaises(ValueError):
            ControlledFillResult(
                application_id="fictional", form_fingerprint="abc",
                started_at="", completed_at="", total_fields=0, attempted_fields=0,
                filled_fields=0, skipped_fields=0, manual_only_fields=0,
                unresolved_fields=0, failed_fields=0, blocked_mutation_requests=[],
                captcha_detected=False, login_required=False,
                submission_attempted=False, submission_completed=True,
                overall_status="FILLED_SAFE_FIELDS",
            )

    def test_browser_backend_contains_no_upload_or_direct_submit_calls(self):
        source = inspect.getsource(PlaywrightControlledFillBrowser)
        self.assertNotIn("set_input_files", source)
        self.assertNotIn("request_submit(", source.casefold())
        self.assertNotIn("press(\"enter\"", source.casefold())

    def test_submission_firewall_blocks_enter_and_programmatic_submit_paths(self):
        self.assertIn("event.key === 'Enter'", _SUBMISSION_FIREWALL_SCRIPT)
        self.assertIn("preventDefault", _SUBMISSION_FIREWALL_SCRIPT)
        self.assertIn("HTMLFormElement.prototype.submit =", _SUBMISSION_FIREWALL_SCRIPT)
        self.assertIn("HTMLFormElement.prototype.requestSubmit =", _SUBMISSION_FIREWALL_SCRIPT)

    def test_cli_preview_writes_only_value_free_private_artifacts(self):
        form, mapping, rendered = self.artifacts([
            {"canonical": "contact_email", "type": "email"}
        ])
        private = self.root / "my-materials" / "application"
        private.mkdir(parents=True)
        package_path = private / "package.json"
        mapping_path = private / "mapping.json"
        form_path = private / "form.json"
        snapshot_path = private / "snapshot.json"
        package_path.write_text(json.dumps(self.package), encoding="utf-8")
        mapping_path.write_text(json.dumps(mapping.to_dict()), encoding="utf-8")
        form_path.write_text(json.dumps(form.to_dict()), encoding="utf-8")
        snapshot_path.write_text(json.dumps(rendered.to_dict()), encoding="utf-8")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "autofill-application",
                "--application-package", str(package_path),
                "--application-id", "application_fictional",
                "--mapping-plan", str(mapping_path),
                "--application-form", str(form_path),
                "--browser-snapshot", str(snapshot_path),
                "--output-root", str(private),
                "--preview",
                "--json",
            ])
        self.assertEqual(code, 0)
        self.assertIn("PREVIEW ONLY", stderr.getvalue())
        self.assertNotIn("avery@example.invalid", stdout.getvalue())
        plan_file = private / "fill_plans" / "application_fictional_fill_plan.json"
        result_file = private / "fill_results" / "application_fictional_fill_result.json"
        log_file = private / "fill_logs" / "application_fictional_fill_log.json"
        self.assertTrue(all(path.is_file() for path in (plan_file, result_file, log_file)))
        self.assertNotIn("avery@example.invalid", plan_file.read_text(encoding="utf-8"))
        self.assertFalse(json.loads(result_file.read_text(encoding="utf-8"))["submission_completed"])


if __name__ == "__main__":
    unittest.main()
