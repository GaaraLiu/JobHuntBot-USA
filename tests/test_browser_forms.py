from __future__ import annotations

import copy
import inspect
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jobhuntbot.answer_bank import AnswerBank, AnswerBankEntry
from jobhuntbot.application_forms.mapping import save_private_json
from jobhuntbot.application_profile import CandidateApplicationProfile
from jobhuntbot.browser_forms import (
    BrowserFieldSnapshot,
    BrowserFormExtractionService,
    BrowserRenderedForm,
    BrowserSessionPolicy,
    PlaywrightReadOnlyBrowser,
    detect_rendered_ats,
    validate_public_browser_url,
)
from jobhuntbot.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "browser_forms" / "rendered_application.json"


class FakeBackend:
    def __init__(self, snapshot: BrowserRenderedForm):
        self.snapshot = snapshot
        self.calls: list[tuple[str, BrowserSessionPolicy]] = []

    def inspect(self, url: str, policy: BrowserSessionPolicy) -> BrowserRenderedForm:
        self.calls.append((url, policy))
        return copy.deepcopy(self.snapshot)


def profile_and_bank():
    def fact(value):
        return {"value": value, "status": "confirmed", "source": "fictional test", "last_verified_at": "2026-01-01", "notes": ""}

    raw = {
        "schema_version": 1,
        "identity": {"legal_first_name": fact("Avery"), "email": fact("avery@example.invalid")},
        "work_authorization": {"authorized_to_work_us": fact(True)},
        "education": [],
        "employment_history": [],
        "links": {},
        "job_preferences": {},
        "application_policies": {},
        "demographics_policy": {},
        "conflicts": [],
        "required_for_ready": [],
    }
    profile = CandidateApplicationProfile(
        1, raw["identity"], raw["work_authorization"], [], [], {}, {}, {}, {}, [], [], raw
    )
    entry = AnswerBankEntry(
        canonical_id="work_authorization_us",
        category="work_authorization",
        answer_type="KNOWN_FACT",
        value=None,
        status="confirmed",
        confidence="HIGH",
        provenance=["fictional test"],
        reusable=True,
        job_dependent=False,
        requires_user_confirmation=False,
        allowed_autofill=True,
        safety_class="STATIC_CONFIRMED",
        profile_fact_path="work_authorization.authorized_to_work_us",
    )
    return profile, AnswerBank(1, {entry.canonical_id: entry}, {"schema_version": 1})


class BrowserFormTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.snapshot = BrowserRenderedForm.from_dict(self.raw)
        self.backend = FakeBackend(self.snapshot)
        self.service = BrowserFormExtractionService(self.backend)

    def outcome(self, **kwargs):
        return self.service.inspect(self.snapshot.requested_url, **kwargs)

    def field(self, field_id):
        return next(item for item in self.outcome().application_form.fields if item.field_id == field_id)

    def test_default_session_policy_is_non_persistent_read_only(self):
        policy = BrowserSessionPolicy()
        self.assertEqual(policy.allowed_http_methods, ("GET", "HEAD", "OPTIONS"))
        self.assertFalse(policy.persist_storage_state)
        self.assertFalse(policy.allow_form_input)
        self.assertFalse(policy.allow_file_upload)
        self.assertFalse(policy.allow_submit)

    def test_policy_rejects_mutation_methods(self):
        with self.assertRaises(ValueError):
            BrowserSessionPolicy(allowed_http_methods=("GET", "POST"))

    def test_policy_rejects_input_upload_or_submission(self):
        for value in ({"allow_form_input": True}, {"allow_file_upload": True}, {"allow_submit": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BrowserSessionPolicy(**value)

    def test_public_url_validation_rejects_local_and_credentials(self):
        for url in ("http://localhost/apply", "http://127.0.0.1/apply", "https://user:secret@example.invalid/apply"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_public_browser_url(url)

    def test_job_search_filters_are_not_application_evidence(self):
        fields = [
            {"dom_type": "search", "label": "Experience", "disabled": False},
            {"dom_type": "select", "label": "Location", "disabled": False},
            {"dom_type": "select", "label": "Area", "disabled": False},
        ]
        self.assertFalse(PlaywrightReadOnlyBrowser._has_application_evidence(fields))

    def test_identity_or_resume_controls_are_application_evidence(self):
        for field in (
            {"dom_type": "email", "label": "Email", "disabled": False},
            {"dom_type": "file", "label": "", "disabled": False},
        ):
            with self.subTest(field=field):
                self.assertTrue(PlaywrightReadOnlyBrowser._has_application_evidence([field]))

    def test_non_application_controls_remain_no_form_when_background_posts_are_blocked(self):
        snapshot = PlaywrightReadOnlyBrowser._snapshot(
            requested_url="https://example.invalid/jobs/123",
            final_url="https://example.invalid/careers",
            page_title="Careers",
            payload={
                "fields": [
                    {
                        "field_id": "search",
                        "section_id": "search",
                        "section_label": "Job Search",
                        "dom_type": "search",
                        "label": "Search Jobs",
                    },
                    {
                        "field_id": "experience",
                        "section_id": "search",
                        "section_label": "Job Search",
                        "dom_type": "select",
                        "label": "Experience",
                    },
                ]
            },
            blocked_requests=[{"method": "POST", "url": "https://example.invalid/analytics"}],
            public_network=[],
            navigation_followed=False,
            error_message="",
        )
        self.assertEqual(snapshot.status, "NO_FORM_DETECTED")

    def test_no_form_status_does_not_generate_mapping_plan(self):
        profile, bank = profile_and_bank()
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.status = "NO_FORM_DETECTED"
        outcome = BrowserFormExtractionService(FakeBackend(snapshot)).inspect(
            snapshot.requested_url,
            profile=profile,
            answer_bank=bank,
        )
        self.assertIsNone(outcome.mapping_plan)

    def test_radiogroup_inherits_question_from_accessible_parent(self):
        raw = [
            {
                "field_id": f"option_{index}",
                "section_id": "questions",
                "section_label": "Questions",
                "label": "",
                "accessible_name": option,
                "dom_type": "radio",
                "required": True,
                "metadata": {
                    "control_label": option,
                    "group_accessible_name": "Are you authorized to work in the United States?",
                    "choice_group_id": "authorization_group",
                },
            }
            for index, option in enumerate(("Yes", "No"), start=1)
        ]
        fields = PlaywrightReadOnlyBrowser._merge_choice_groups(raw)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0].field_id, "authorization_group")
        self.assertEqual(fields[0].label, "Are you authorized to work in the United States?")
        self.assertEqual(fields[0].options, ["Yes", "No"])
        self.assertTrue(fields[0].required)

    def test_custom_card_inherits_question_from_structural_heading(self):
        raw = {
            "field_id": "fictional_card_field",
            "section_id": "questions",
            "section_label": "Questions",
            "label": "fictional_card_field",
            "accessible_name": "",
            "dom_type": "textarea",
            "metadata": {
                "control_label": "",
                "group_heading": "Describe a fictional project.",
            },
        }
        field = PlaywrightReadOnlyBrowser._merge_choice_groups([raw])[0]
        self.assertEqual(field.label, "Describe a fictional project.")
        self.assertEqual(field.metadata["question_context_source"], "group_heading")

    def test_option_text_alone_does_not_become_question_text(self):
        raw = {
            "field_id": "ambiguous_choice",
            "section_id": "questions",
            "section_label": "Questions",
            "label": "",
            "accessible_name": "Yes",
            "dom_type": "radio",
            "required": True,
            "metadata": {"control_label": "Yes"},
        }
        field = PlaywrightReadOnlyBrowser._merge_choice_groups([raw])[0]
        self.assertEqual(field.label, "")
        self.assertEqual(field.accessible_name, "")
        self.assertEqual(field.options, ["Yes"])

    def test_legal_choice_uses_group_context_but_remains_manual_only(self):
        raw = {
            "field_id": "privacy_choice",
            "section_id": "questions",
            "section_label": "Questions",
            "label": "",
            "accessible_name": "Yes, I agree",
            "dom_type": "checkbox",
            "required": True,
            "metadata": {
                "control_label": "Yes, I agree",
                "group_heading": "Privacy policy acknowledgment",
                "choice_group_id": "privacy_acknowledgment",
            },
        }
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.fields = PlaywrightReadOnlyBrowser._merge_choice_groups([raw])
        profile, bank = profile_and_bank()
        outcome = BrowserFormExtractionService(FakeBackend(snapshot)).inspect(
            snapshot.requested_url,
            profile=profile,
            answer_bank=bank,
        )
        plan = outcome.mapping_plan.fields[0]
        self.assertEqual((plan.action, plan.safety_class), ("MANUAL_ONLY", "MANUAL_ONLY"))

    def test_native_text_input_extraction(self):
        self.assertEqual(self.field("first_name").field_type, "text")

    def test_textarea_extraction(self):
        self.assertEqual(self.field("summary").field_type, "textarea")

    def test_select_options(self):
        self.assertEqual(self.field("country").options, ["Exampleland", "Sample Republic"])

    def test_aria_combobox_options(self):
        field = self.field("location")
        self.assertEqual(field.field_type, "select")
        self.assertEqual(field.metadata["accessible_name"], "Preferred location")

    def test_radio_options(self):
        self.assertEqual(self.field("authorized").options, ["Yes", "No"])

    def test_checkbox_extraction(self):
        self.assertEqual(self.field("newsletter").field_type, "checkbox")

    def test_required_flag(self):
        self.assertTrue(self.field("first_name").required)

    def test_file_input_becomes_resume(self):
        self.assertEqual(self.field("resume").field_type, "resume")

    def test_hidden_and_disabled_are_preserved_as_metadata(self):
        field = self.field("internal")
        self.assertTrue(field.metadata["browser_hidden"])
        self.assertTrue(field.metadata["browser_disabled"])

    def test_section_grouping(self):
        form = self.outcome().application_form
        self.assertIn("Personal Information", {item.label for item in form.sections})

    def test_repeat_group_and_index(self):
        field = self.field("school")
        self.assertEqual((field.repeat_group, field.repeat_index), ("education", 0))

    def test_url_ats_detection_covers_all_five(self):
        cases = {
            "https://job-boards.greenhouse.io/example/jobs/1": "greenhouse",
            "https://jobs.lever.co/example/1": "lever",
            "https://jobs.ashbyhq.com/example/1": "ashby",
            "https://jobs.smartrecruiters.com/Example/1": "smartrecruiters",
            "https://example.wd5.myworkdayjobs.com/External/job/1": "workday",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_rendered_ats(url), expected)

    def test_dom_ats_detection_requires_unambiguous_signature(self):
        self.assertEqual(detect_rendered_ats("https://careers.example.invalid", dom_markers=["data-automation-id workday"]), "workday")
        self.assertEqual(detect_rendered_ats("https://careers.example.invalid", dom_markers=["greenhouse lever"]), "unknown")

    def test_legal_consent_uses_existing_manual_only_mapping(self):
        profile, bank = profile_and_bank()
        outcome = self.outcome(profile=profile, answer_bank=bank)
        plan = next(item for item in outcome.mapping_plan.fields if item.field_id == "accuracy")
        self.assertEqual((plan.canonical_question_id, plan.action, plan.safety_class), ("accuracy_attestation", "MANUAL_ONLY", "MANUAL_ONLY"))

    def test_login_wall_is_preserved_and_blocks_form(self):
        self.snapshot.status = "LOGIN_REQUIRED"
        self.snapshot.login_wall = True
        outcome = self.outcome()
        self.assertEqual(outcome.application_form.detection_status, "LOGIN_REQUIRED")

    def test_captcha_status_is_preserved(self):
        self.snapshot.status = "BLOCKED_BY_ANTI_BOT"
        self.snapshot.anti_bot = True
        outcome = self.outcome()
        self.assertEqual(outcome.application_form.detection_status, "BLOCKED_BY_ANTI_BOT")

    def test_multi_step_metadata_stops_without_advancing(self):
        form = self.outcome().application_form
        self.assertEqual(form.metadata["current_step"], "Step 1 of 2")
        self.assertTrue(form.metadata["later_steps_unavailable_without_submission"])

    def test_backend_contract_has_no_candidate_interaction(self):
        self.outcome()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertFalse(hasattr(self.backend, "type"))
        self.assertFalse(hasattr(self.backend, "upload"))
        self.assertFalse(hasattr(self.backend, "submit"))

    def test_playwright_backend_contains_no_input_upload_or_click_calls(self):
        source = inspect.getsource(PlaywrightReadOnlyBrowser)
        for forbidden in (".fill(", ".type(", ".click(", ".check(", "set_input_files", "select_option"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_browser_to_phase31_mapping_integration(self):
        profile, bank = profile_and_bank()
        outcome = self.outcome(profile=profile, answer_bank=bank, application_id="fictional-1")
        self.assertIsNotNone(outcome.mapping_plan)
        self.assertEqual(outcome.mapping_plan.total_fields, len(outcome.application_form.fields))
        first = next(item for item in outcome.mapping_plan.fields if item.field_id == "first_name")
        self.assertEqual(first.action, "AUTO_READY_FUTURE")

    def test_fingerprint_is_stable_for_same_rendered_structure(self):
        self.assertEqual(self.outcome().application_form.fingerprint, self.outcome().application_form.fingerprint)

    def test_fingerprint_changes_when_rendered_structure_changes(self):
        before = self.outcome().application_form.fingerprint
        self.snapshot.fields[0].required = False
        after = self.outcome().application_form.fingerprint
        self.assertNotEqual(before, after)

    def test_private_output_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            private = Path(temp) / "my-materials" / "application" / "browser_forms" / "form.json"
            save_private_json(private, self.outcome().application_form.to_dict())
            self.assertTrue(private.exists())
            with self.assertRaises(ValueError):
                save_private_json(Path(temp) / "public" / "form.json", {})

    def test_cli_prominently_reports_read_only_and_uses_fake_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "my-materials" / "application" / "browser_forms"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("jobhuntbot.cli.PlaywrightReadOnlyBrowser", return_value=self.backend):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([
                        "inspect-application-form",
                        "--url", self.snapshot.requested_url,
                        "--output-dir", str(output),
                        "--json",
                    ])
            self.assertEqual(code, 0)
            self.assertIn("READ ONLY", stderr.getvalue())
            self.assertIn('"candidate_data_typed": false', stdout.getvalue())
            self.assertTrue(any(output.glob("*_application_form.json")))


if __name__ == "__main__":
    unittest.main()
