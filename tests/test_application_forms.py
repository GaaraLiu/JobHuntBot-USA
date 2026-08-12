from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from jobhuntbot.answer_bank import load_answer_bank
from jobhuntbot.application_forms import (
    ApplicationField,
    ApplicationForm,
    ApplicationFormMapper,
    FieldConceptMapper,
    FormContext,
    FormSection,
    create_form_adapter,
    detect_application_ats,
)
from jobhuntbot.application_forms.mapping import load_application_form, save_private_json
from jobhuntbot.application_forms.base import normalize_label
from jobhuntbot.application_profile import load_application_profile
from jobhuntbot.cli import main


FIXTURES = Path(__file__).parent / "fixtures" / "forms"


def fact(value=None, status="unknown", source=""):
    return {
        "value": value,
        "status": status,
        "source": source,
        "last_verified_at": "2026-01-01" if status == "confirmed" else "",
        "notes": "",
    }


def profile_value():
    return {
        "schema_version": 1,
        "identity": {
            "legal_first_name": fact("Avery", "confirmed", "fictional user"),
            "legal_middle_name": fact("NONE", "confirmed", "fictional user"),
            "legal_last_name": fact("Example", "confirmed", "fictional user"),
            "preferred_name": fact("Avery Example", "confirmed", "fictional user"),
            "email": fact("avery@example.invalid", "confirmed", "fictional user"),
            "phone": fact("+1 555-0100", "confirmed", "fictional user"),
            "current_address": fact("1 Example Way", "confirmed", "fictional user"),
            "current_city": fact("Example City", "confirmed", "fictional user"),
            "current_state": fact("Example State", "confirmed", "fictional user"),
            "postal_code": fact("00000", "confirmed", "fictional user"),
            "current_country": fact("Exampleland", "confirmed", "fictional user"),
        },
        "work_authorization": {
            "authorized_to_work_us": fact(True, "confirmed", "fictional user"),
            "requires_sponsorship_now": fact(False, "confirmed", "fictional user"),
            "requires_sponsorship_future": fact(False, "confirmed", "fictional user"),
            "us_citizen": fact(),
            "permanent_resident": fact(),
            "security_clearance": fact(),
        },
        "education": [{
            "institution": fact("Example University", "confirmed", "fictional user"),
            "degree": fact("Example Degree", "confirmed", "fictional user"),
            "graduation_date": fact("2025-01", "confirmed", "fictional user"),
        }],
        "employment_history": [{
            "employer": fact("Example Employer", "confirmed", "fictional user"),
            "title": fact("Example Analyst", "confirmed", "fictional user"),
        }],
        "certifications": [{"name": "Example Credential", "status": "confirmed", "source": "fictional user"}],
        "links": {
            "linkedin": fact("https://example.invalid/linkedin", "confirmed", "fictional user"),
            "github": fact(),
            "portfolio": fact(),
        },
        "job_preferences": {
            "salary_response_policy": fact({"strategy": "negotiable_preferred"}, "confirmed", "fictional user"),
            "location_policy": fact({"local_acceptable": ["Example City"], "relocation_allowed": []}, "confirmed", "fictional user"),
        },
        "application_policies": {
            "years_of_experience": fact(),
            "years_python_experience": fact(),
            "driver_license": fact(True, "confirmed", "fictional user"),
        },
        "demographics_policy": {
            "default_response": fact("prefer_not_to_answer", "confirmed", "fictional user"),
            "allowed_autofill": fact(False, "confirmed", "fictional user"),
        },
        "conflicts": [],
        "required_for_ready": [],
    }


def entry(canonical_id, path="", **overrides):
    value = {
        "canonical_question_id": canonical_id,
        "category": "test",
        "answer_type": "KNOWN_FACT",
        "value": None,
        "status": "confirmed",
        "confidence": "HIGH",
        "provenance": ["fictional user"],
        "reusable": True,
        "job_dependent": False,
        "requires_user_confirmation": False,
        "allowed_auto_fill": True,
        "safety_class": "STATIC_CONFIRMED",
        "profile_fact_path": path,
        "notes": "",
    }
    value.update(overrides)
    return value


def bank_value():
    unknown = {
        "answer_type": "NEVER_GUESS", "status": "unknown", "confidence": "",
        "provenance": [], "allowed_auto_fill": False, "safety_class": "NEVER_GUESS",
    }
    job_dependent = {
        "answer_type": "JOB_DEPENDENT", "status": "confirmed", "reusable": False,
        "job_dependent": True, "requires_user_confirmation": True,
        "allowed_auto_fill": False, "safety_class": "JOB_DEPENDENT",
    }
    return {
        "schema_version": 1,
        "entries": [
            entry("work_authorization_us", "work_authorization.authorized_to_work_us"),
            entry("sponsorship_now", "work_authorization.requires_sponsorship_now"),
            entry("sponsorship_future", "work_authorization.requires_sponsorship_future"),
            entry("us_citizen", "work_authorization.us_citizen", **unknown),
            entry("security_clearance", "work_authorization.security_clearance", **unknown),
            entry("driver_license", "application_policies.driver_license"),
            entry("desired_salary", **job_dependent),
            entry("willing_to_relocate", **job_dependent),
            entry("years_of_experience", "application_policies.years_of_experience", **unknown),
            entry("years_python_experience", "application_policies.years_python_experience", **unknown),
            entry(
                "gender",
                answer_type="OPTIONAL_PREFER_NOT",
                status="unknown",
                confidence="",
                provenance=[],
                reusable=False,
                allowed_auto_fill=False,
                safety_class="OPTIONAL_PREFER_NOT_TO_ANSWER",
            ),
        ],
    }


class ApplicationFormTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / "my-materials" / "application"
        self.private.mkdir(parents=True)
        profile_path = self.private / "profile.json"
        bank_path = self.private / "bank.json"
        profile_path.write_text(json.dumps(profile_value()), encoding="utf-8")
        bank_path.write_text(json.dumps(bank_value()), encoding="utf-8")
        self.profile = load_application_profile(profile_path)
        self.bank = load_answer_bank(bank_path)
        self.mapper = ApplicationFormMapper(self.profile, self.bank)

    def tearDown(self):
        self.temp.cleanup()

    def field(self, label, field_type="text", required=True, **kwargs):
        return ApplicationField(
            field_id=kwargs.pop("field_id", "field"),
            section=kwargs.pop("section", "Application"),
            label=label,
            normalized_label=label.casefold(),
            field_type=field_type,
            required=required,
            **kwargs,
        )

    def form(self, fields):
        value = ApplicationForm("fixture", "unknown", "", "job-1", "Example", "Role", "2026-01-01", [FormSection("application", "Application", fields)])
        value.compute_fingerprint()
        return value

    def plan_for(self, field, **kwargs):
        return self.mapper.build_plan(self.form([field]), job=kwargs.get("job", {}), selected_resume=kwargs.get("selected_resume", {})).fields[0]

    def test_detects_all_supported_ats_urls(self):
        cases = {
            "https://boards.greenhouse.io/example/jobs/1": "greenhouse",
            "https://jobs.lever.co/example/1": "lever",
            "https://jobs.ashbyhq.com/example/1": "ashby",
            "https://jobs.smartrecruiters.com/Example/1": "smartrecruiters",
            "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/1": "workday",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_application_ats(url), expected)

    def test_known_phase2_source_has_precedence(self):
        self.assertEqual(detect_application_ats("https://example.invalid/job", "lever"), "lever")

    def test_unsupported_url_remains_unknown(self):
        self.assertEqual(detect_application_ats("https://careers.example.invalid/job"), "unknown")

    def test_html_parser_extracts_text_required_and_select_options(self):
        html = (FIXTURES / "generic_form.html").read_text(encoding="utf-8")
        form = create_form_adapter("unknown").parse_html(html, FormContext("fixture", "unknown"))
        email = next(item for item in form.fields if item.field_id == "email")
        country = next(item for item in form.fields if item.field_id == "country")
        self.assertEqual((email.field_type, email.required), ("email", True))
        self.assertEqual(country.options, ["Exampleland", "Sample Republic"])

    def test_html_parser_groups_radio_options(self):
        html = (FIXTURES / "generic_form.html").read_text(encoding="utf-8")
        form = create_form_adapter("unknown").parse_html(html, FormContext("fixture", "unknown"))
        radio = next(item for item in form.fields if item.field_id == "relocate")
        self.assertEqual(radio.field_type, "radio")
        self.assertEqual(len(radio.options), 2)

    def test_html_without_controls_reports_a_blocker(self):
        form = create_form_adapter("greenhouse").parse_html(
            "<html><body><button>Apply</button></body></html>",
            FormContext("fixture", "greenhouse"),
        )
        self.assertEqual(form.detection_status, "no_fields_detected")
        self.assertTrue(form.blockers)

    def test_generic_page_search_control_is_not_an_application_form(self):
        form = create_form_adapter("greenhouse").parse_html(
            '<form><label for="search">Search jobs</label><input id="search"></form>',
            FormContext("public_read_only_get", "greenhouse"),
        )
        self.assertEqual(form.detection_status, "no_application_form_evidence")
        self.assertEqual(len(form.fields), 1)
        self.assertTrue(form.blockers)

    def test_each_supported_adapter_parses_sanitized_fixture(self):
        names = {
            "greenhouse": "greenhouse_form.json",
            "lever": "lever_form.json",
            "ashby": "ashby_form.json",
            "smartrecruiters": "smartrecruiters_form.json",
            "workday": "workday_form.json",
        }
        for ats, filename in names.items():
            with self.subTest(ats=ats):
                payload = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
                form = create_form_adapter(ats).parse_json(payload, FormContext("fixture", ats))
                self.assertEqual(form.ats, ats)
                self.assertGreater(len(form.fields), 0)
                self.assertEqual(form.detection_status, "detected")

    def test_empty_structured_form_is_explicitly_blocked(self):
        context = FormContext("fixture", "greenhouse")
        form = create_form_adapter("greenhouse").parse_json({"questions": []}, context)
        self.assertEqual(form.detection_status, "no_fields_detected")
        self.assertTrue(form.blockers)

    def test_work_authorization_is_high_confidence(self):
        plan = self.plan_for(self.field("Are you legally authorized to work in the United States?", "boolean"))
        self.assertEqual((plan.canonical_question_id, plan.mapping_confidence), ("work_authorization_us", "HIGH"))
        self.assertEqual(plan.action, "AUTO_READY_FUTURE")

    def test_required_marker_normalization_is_presentation_only(self):
        cases = {
            "Full name✱": "full name",
            "Email✱": "email",
            "Phone ✱": "phone",
            "Email *": "email",
            "Desired salary (required)": "desired salary",
            "Phone - required field": "phone",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_label(raw), expected)
        self.assertEqual(normalize_label("Is sponsorship required"), "is sponsorship required")

    def test_required_flag_survives_label_cleanup(self):
        form = create_form_adapter("unknown").parse_json(
            {"sections": [{"id": "application", "label": "Application", "fields": [
                {"id": "email", "label": "Email✱", "type": "email", "required": True}
            ]}]},
            FormContext("fixture", "unknown"),
        )
        field = form.fields[0]
        self.assertEqual(field.normalized_label, "email")
        self.assertTrue(field.required)

    def test_basic_identity_and_contact_fields_map_high_confidence(self):
        cases = {
            "Full name✱": "legal_full_name",
            "First name": "legal_first_name",
            "Middle name": "legal_middle_name",
            "Last name": "legal_last_name",
            "Preferred name": "preferred_name",
            "Email✱": "contact_email",
            "Phone ✱": "contact_phone",
        }
        for label, canonical_id in cases.items():
            with self.subTest(label=label):
                plan = self.plan_for(self.field(label))
                self.assertEqual((plan.canonical_question_id, plan.mapping_confidence), (canonical_id, "HIGH"))
                self.assertEqual(plan.action, "AUTO_READY_FUTURE")

    def test_full_name_uses_legal_name_composition_not_preferred_name(self):
        full = self.plan_for(self.field("Full name"))
        preferred = self.plan_for(self.field("Preferred name"))
        self.assertEqual(full.canonical_question_id, "legal_full_name")
        self.assertIn("identity.legal_first_name", full.source_reference)
        self.assertIn("identity.legal_last_name", full.source_reference)
        self.assertNotEqual(full.canonical_question_id, preferred.canonical_question_id)

    def test_future_sponsorship_is_distinct(self):
        plan = self.plan_for(self.field("Will you now or in the future require sponsorship?", "boolean"))
        self.assertEqual(plan.canonical_question_id, "sponsorship_future")

    def test_sponsorship_now_future_authorization_and_visa_type_stay_distinct(self):
        cases = {
            "Are you legally authorized to work in the United States?": "work_authorization_us",
            "Do you currently require visa sponsorship?": "sponsorship_now",
            "Will you now or in the future require sponsorship?": "sponsorship_future",
            "Current visa type": "visa_type",
        }
        for label, canonical_id in cases.items():
            with self.subTest(label=label):
                plan = self.plan_for(self.field(label, "select"))
                self.assertEqual(plan.canonical_question_id, canonical_id)
        visa = self.plan_for(self.field("Current visa type", "select"))
        self.assertEqual((visa.action, visa.answer_status), ("UNRESOLVED", "no_answer_bank_entry"))

    def test_visa_type_is_not_derived_from_permanent_resident_status(self):
        value = profile_value()
        value["work_authorization"]["permanent_resident"] = fact(True, "confirmed", "fictional user")
        profile_path = self.private / "permanent-resident-profile.json"
        profile_path.write_text(json.dumps(value), encoding="utf-8")
        mapper = ApplicationFormMapper(load_application_profile(profile_path), self.bank)
        plan = mapper.build_plan(self.form([self.field("Current visa type", "select")])).fields[0]
        self.assertEqual(plan.canonical_question_id, "visa_type")
        self.assertEqual((plan.action, plan.answer_status), ("UNRESOLVED", "no_answer_bank_entry"))

    def test_legal_group_context_is_manual_and_ambiguous_yes_is_unresolved(self):
        legal = self.plan_for(self.field("Privacy policy acknowledgment: Yes, I agree", "checkbox"))
        ambiguous = self.plan_for(self.field("Yes", "radio"))
        self.assertEqual((legal.action, legal.safety_class), ("MANUAL_ONLY", "MANUAL_ONLY"))
        self.assertEqual((ambiguous.canonical_question_id, ambiguous.action), ("", "UNRESOLVED"))

    def test_citizenship_is_not_collapsed_into_authorization(self):
        plan = self.plan_for(self.field("Are you a U.S. citizen?", "boolean"))
        self.assertEqual(plan.canonical_question_id, "us_citizen")
        self.assertEqual(plan.safety_class, "NEVER_GUESS")
        self.assertEqual(plan.action, "UNRESOLVED")

    def test_security_clearance_mapping_preserves_never_guess(self):
        plan = self.plan_for(self.field("Security clearance level", "select"))
        self.assertEqual((plan.canonical_question_id, plan.mapping_confidence), ("security_clearance", "HIGH"))
        self.assertEqual((plan.safety_class, plan.action), ("NEVER_GUESS", "UNRESOLVED"))

    def test_driver_license_confirmed_can_be_future_ready(self):
        plan = self.plan_for(self.field("Do you possess a valid driver's license?", "boolean"))
        self.assertEqual((plan.canonical_question_id, plan.action), ("driver_license", "AUTO_READY_FUTURE"))

    def test_salary_is_job_dependent_and_not_chosen(self):
        plan = self.plan_for(self.field("Expected compensation", "number"), job={"salary": {"minimum": 50, "maximum": 70}})
        self.assertEqual((plan.canonical_question_id, plan.safety_class), ("desired_salary", "JOB_DEPENDENT"))
        self.assertEqual(plan.action, "UNRESOLVED")

    def test_relocation_uses_job_context_but_still_requires_review(self):
        plan = self.plan_for(self.field("Are you willing to relocate?", "boolean"), job={"location": "Example City"})
        self.assertEqual(plan.canonical_question_id, "willing_to_relocate")
        self.assertEqual(plan.action, "USER_CONFIRMATION")

    def test_total_experience_remains_unresolved(self):
        plan = self.plan_for(self.field("Total years of professional experience", "number"))
        self.assertEqual(plan.canonical_question_id, "years_of_experience")
        self.assertEqual(plan.action, "UNRESOLVED")

    def test_python_experience_is_domain_specific(self):
        plan = self.plan_for(self.field("How many years of Python experience?", "number"))
        self.assertEqual(plan.canonical_question_id, "years_python_experience")
        self.assertEqual(plan.action, "UNRESOLVED")

    def test_power_bi_experience_does_not_become_total_experience(self):
        plan = self.plan_for(self.field("Years of Power BI experience", "number"))
        self.assertEqual(plan.canonical_question_id, "years_power_bi_experience")
        self.assertEqual(plan.action, "UNRESOLVED")

    def test_optional_demographic_field_is_isolated(self):
        plan = self.plan_for(self.field("Gender", "select", required=False, options=["Prefer not to answer"]))
        self.assertEqual(plan.safety_class, "OPTIONAL_PREFER_NOT_TO_ANSWER")
        self.assertEqual(plan.action, "OPTIONAL_SKIP")

    def test_legal_attestation_is_manual_only(self):
        plan = self.plan_for(self.field("I certify that the information provided is accurate", "checkbox"))
        self.assertEqual(plan.action, "MANUAL_ONLY")
        self.assertEqual(plan.safety_class, "MANUAL_ONLY")

    def test_resume_file_is_detected_but_never_uploaded(self):
        plan = self.plan_for(self.field("Resume", "resume"), selected_resume={"verified_exists": True})
        self.assertEqual((plan.canonical_question_id, plan.action), ("resume_upload", "MANUAL_ONLY"))
        self.assertEqual(plan.answer_status, "selected_resume_reference_available")

    def test_cover_letter_and_other_files_are_structural_only(self):
        cover = self.plan_for(self.field("Cover letter", "cover_letter", required=False))
        transcript = self.plan_for(self.field("Transcript", "file", required=False))
        self.assertEqual((cover.action, transcript.action), ("MANUAL_ONLY", "MANUAL_ONLY"))

    def test_repeated_education_section_maps_to_private_group(self):
        field = self.field("School", repeat_group="education", repeat_index=0)
        plan = self.plan_for(field)
        self.assertEqual(plan.canonical_question_id, "education.institution")
        self.assertEqual(plan.action, "USER_CONFIRMATION")

    def test_repeated_employment_section_maps_to_private_group(self):
        field = self.field("Company", repeat_group="employment", repeat_index=0)
        plan = self.plan_for(field)
        self.assertEqual(plan.canonical_question_id, "employment.employer")
        self.assertEqual(plan.action, "USER_CONFIRMATION")

    def test_address_decomposition_uses_confirmed_components(self):
        for label, expected in (("Street address", "address.street"), ("City", "address.city"), ("Postal code", "address.postal_code"), ("Country", "address.country")):
            with self.subTest(label=label):
                plan = self.plan_for(self.field(label))
                self.assertEqual(plan.canonical_question_id, expected)
                self.assertEqual(plan.action, "AUTO_READY_FUTURE")

    def test_unmapped_required_custom_question_is_unresolved(self):
        plan = self.plan_for(self.field("Describe your favorite imaginary constellation", "textarea"))
        self.assertEqual((plan.mapping_confidence, plan.action), ("UNMAPPED", "UNRESOLVED"))

    def test_vague_work_authorization_is_low_confidence_and_not_inferred(self):
        plan = self.plan_for(self.field("Work authorization", "text"))
        self.assertEqual(plan.mapping_confidence, "LOW")
        self.assertEqual(plan.canonical_question_id, "")
        self.assertEqual(plan.safety_class, "NEVER_GUESS")

    def test_mapping_plan_counts_and_readiness(self):
        fields = [
            self.field("Legal first name", field_id="first"),
            self.field("Expected compensation", "number", field_id="salary"),
            self.field("Portfolio", "text", required=False, field_id="portfolio"),
        ]
        plan = self.mapper.build_plan(self.form(fields), job={"salary": {"minimum": 1, "maximum": 2}})
        self.assertEqual((plan.total_fields, plan.required_fields), (3, 2))
        self.assertEqual(plan.required_unresolved, 1)
        self.assertEqual(plan.package_readiness, "NEEDS_USER_INPUT")

    def test_optional_unmapped_field_does_not_block_readiness(self):
        plan = self.mapper.build_plan(self.form([self.field("Favorite constellation", required=False)]))
        self.assertEqual(plan.optional_skip, 1)
        self.assertEqual(plan.package_readiness, "READY")

    def test_required_manual_field_prevents_ready(self):
        plan = self.mapper.build_plan(self.form([self.field("Electronic signature", "signature")]))
        self.assertEqual(plan.manual_only, 1)
        self.assertEqual(plan.package_readiness, "NEEDS_USER_INPUT")

    def test_fingerprint_is_stable_for_same_structure(self):
        first = self.form([self.field("Email", "email")])
        second = self.form([self.field("Email", "email")])
        first.application_url = "https://one.example.invalid"
        second.application_url = "https://two.example.invalid"
        self.assertEqual(first.compute_fingerprint(), second.compute_fingerprint())

    def test_fingerprint_changes_with_required_or_options(self):
        first = self.form([self.field("Country", "select", required=False, options=["A"])])
        second = self.form([self.field("Country", "select", required=True, options=["A", "B"])])
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_fingerprint_does_not_accept_candidate_answer_values(self):
        form = self.form([self.field("Email", "email")])
        baseline = form.fingerprint
        form.metadata["candidate_answer_for_test"] = "secret@example.invalid"
        self.assertEqual(form.compute_fingerprint(), baseline)

    def test_private_save_rejects_non_private_path(self):
        with self.assertRaises(ValueError):
            save_private_json(self.root / "form.json", {"test": True})

    def test_private_save_and_load_canonical_form(self):
        form = self.form([self.field("Email", "email")])
        path = save_private_json(self.private / "forms" / "form.json", form.to_dict())
        loaded = load_application_form(path)
        self.assertEqual(loaded.fingerprint, form.fingerprint)

    def test_cli_inspects_saved_form_without_live_network(self):
        output_path = self.private / "forms" / "greenhouse.json"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "application-form-inspect",
                "--input", str(FIXTURES / "greenhouse_form.json"),
                "--application-url", "https://boards.greenhouse.io/example/jobs/1",
                "--output", str(output_path),
            ])
        self.assertEqual(code, 0)
        self.assertTrue(output_path.exists())
        self.assertIn("ATS: greenhouse", output.getvalue())


if __name__ == "__main__":
    unittest.main()
