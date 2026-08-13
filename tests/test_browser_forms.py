from __future__ import annotations

import copy
import inspect
import io
import json
import sys
import tempfile
import threading
import types
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


class FakeRoute:
    def __init__(self):
        self.action = ""

    def abort(self, reason):
        self.action = f"abort:{reason}"

    def continue_(self):
        self.action = "continue"


class FakeRequest:
    def __init__(self, method="POST", url="https://example.invalid/session?secret=redacted"):
        self.method = method
        self.url = url


class FakeLocator:
    def __init__(self, controls):
        self.controls = controls

    def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]


class FakeControl:
    def __init__(self, page, label):
        self.page = page
        self.label = label

    def is_visible(self):
        return True

    def click(self):
        self.page.clicked.append(self.label)
        if self.label == "Apply Manually":
            self.page.state = 1


class FakeWorkdayPage:
    def __init__(self, payloads):
        self.payloads = payloads
        self.state = 0
        self.clicked = []
        self.closed = False
        self.url = "https://example.wd5.myworkdayjobs.com/External/apply/job/1"

    def set_default_timeout(self, _timeout):
        pass

    def goto(self, *_args, **_kwargs):
        pass

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, _timeout):
        pass

    def evaluate(self, _script):
        return copy.deepcopy(self.payloads[self.state])

    def get_by_role(self, role, name):
        labels = self.payloads[self.state].get("action_labels", [])
        controls = [FakeControl(self, label) for label in labels if role == "button" and name.fullmatch(label)]
        return FakeLocator(controls)

    def on(self, *_args):
        pass

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.route_handler = None

    def new_page(self):
        return self.page

    def route(self, _pattern, handler):
        self.route_handler = handler

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False

    def new_context(self, **_kwargs):
        return self.context

    def close(self):
        self.closed = True


class FakePlaywrightManager:
    def __init__(self, browser):
        self.chromium = types.SimpleNamespace(launch=lambda **_kwargs: browser)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakePlaywrightTimeout(Exception):
    pass


def fake_workday_payloads():
    common = {
        "page_title": "Fictional Workday Application",
        "anti_bot": False,
        "password_input": False,
        "already_have_account": False,
        "current_step": "",
        "step_count": None,
        "later_steps_unavailable_without_submission": False,
        "dom_markers": ["data-automation-id workday"],
        "apply_href": "",
    }
    chooser = {
        **common,
        "fields": [],
        "login_wall": False,
        "headings": ["Start Your Application"],
        "action_labels": [
            "Autofill with Resume",
            "Apply Manually",
            "Use My Last Application",
            "Apply With LinkedIn",
        ],
        "workday_application_chooser": True,
    }
    login = {
        **common,
        "fields": [],
        "login_wall": True,
        "password_input": True,
        "headings": ["Sign In"],
        "action_labels": ["Sign In", "Create Account"],
        "workday_application_chooser": False,
    }
    form = {
        **common,
        "fields": [
            {
                "field_id": "email",
                "section_id": "identity",
                "section_label": "My Information",
                "label": "Email",
                "dom_type": "email",
            }
        ],
        "login_wall": False,
        "headings": ["My Information"],
        "action_labels": ["Save and Continue"],
        "workday_application_chooser": False,
    }
    return [chooser, login, form]


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

    def test_manual_auth_requires_headed_browser(self):
        with self.assertRaisesRegex(ValueError, "--headed"):
            BrowserSessionPolicy(allow_manual_auth_handoff=True)
        policy = BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True)
        self.assertEqual(policy.allowed_http_methods, ("GET", "HEAD", "OPTIONS"))

    def test_workday_chooser_requires_heading_and_exact_manual_action(self):
        payload = fake_workday_payloads()[0]
        self.assertTrue(PlaywrightReadOnlyBrowser._is_workday_application_chooser(payload))
        self.assertFalse(PlaywrightReadOnlyBrowser._is_workday_application_chooser({
            "headings": ["Job Details"], "action_labels": ["Apply Manually"]
        }))

    def test_only_exact_apply_manually_is_selected(self):
        page = FakeWorkdayPage(fake_workday_payloads())
        self.assertTrue(PlaywrightReadOnlyBrowser._choose_workday_manual_application(page))
        self.assertEqual(page.clicked, ["Apply Manually"])
        self.assertNotIn("Apply With LinkedIn", page.clicked)
        self.assertNotIn("Autofill with Resume", page.clicked)
        self.assertNotIn("Use My Last Application", page.clicked)

    def test_auth_gate_detects_modern_workday_states_without_broad_text_match(self):
        cases = (
            {"password_input": True},
            {"headings": ["Sign In"]},
            {"headings": ["Create an Account"]},
            {"already_have_account": True, "action_labels": ["Log In"]},
            {"auth_prompt": True, "action_labels": ["Create Account"]},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertTrue(PlaywrightReadOnlyBrowser._is_authentication_gate(payload))
        self.assertFalse(PlaywrightReadOnlyBrowser._is_authentication_gate({
            "headings": ["Benefits"], "action_labels": ["Sign In to newsletter"]
        }))

    def test_firewall_is_temporarily_human_controlled_then_rearmed(self):
        policy = BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True)
        state = {"manual_auth_active": False}
        blocked = []
        route = FakeRoute()
        PlaywrightReadOnlyBrowser._route_request(route, FakeRequest(), policy, state, blocked)
        self.assertEqual(route.action, "abort:blockedbyclient")
        self.assertEqual(blocked[0]["url"], "https://example.invalid/session")

        state["manual_auth_active"] = True
        route = FakeRoute()
        PlaywrightReadOnlyBrowser._route_request(route, FakeRequest(), policy, state, blocked)
        self.assertEqual(route.action, "continue")

        state["manual_auth_active"] = False
        route = FakeRoute()
        PlaywrightReadOnlyBrowser._route_request(route, FakeRequest(), policy, state, blocked)
        self.assertEqual(route.action, "abort:blockedbyclient")

    def test_manual_handoff_pumps_routes_and_rearms_before_return(self):
        policy = BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True)
        state = {"manual_auth_active": False}
        blocked = []
        auth_request_seen = threading.Event()

        class PumpingPage:
            def wait_for_timeout(self, _timeout):
                route = FakeRoute()
                PlaywrightReadOnlyBrowser._route_request(
                    route,
                    FakeRequest(url="https://example.invalid/auth?password=must-not-be-logged"),
                    policy,
                    state,
                    blocked,
                )
                if route.action == "continue":
                    auth_request_seen.set()

        def confirm(_prompt):
            self.assertTrue(auth_request_seen.wait(1))
            return ""

        backend = PlaywrightReadOnlyBrowser(
            manual_auth_confirmation=confirm,
            manual_auth_notice=lambda _message: None,
        )
        result = backend._perform_manual_auth_handoff(PumpingPage(), state)

        self.assertTrue(result["manual_auth_resumed"])
        self.assertTrue(result["manual_auth_firewall_rearmed"])
        self.assertFalse(state["manual_auth_active"])
        self.assertEqual(blocked, [])

        route = FakeRoute()
        PlaywrightReadOnlyBrowser._route_request(route, FakeRequest(), policy, state, blocked)
        self.assertEqual(route.action, "abort:blockedbyclient")

    def test_manual_handoff_cancellation_rearms_firewall(self):
        state = {"manual_auth_active": False}
        backend = PlaywrightReadOnlyBrowser(
            manual_auth_confirmation=lambda _prompt: "cancel",
            manual_auth_notice=lambda _message: None,
        )
        result = backend._perform_manual_auth_handoff(FakeWorkdayPage(fake_workday_payloads()), state)
        self.assertTrue(result["manual_auth_cancelled"])
        self.assertTrue(result["manual_auth_firewall_rearmed"])
        self.assertFalse(state["manual_auth_active"])

    def test_auth_request_contents_are_never_inspected_or_logged(self):
        policy = BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True)
        blocked = []
        route = FakeRoute()
        PlaywrightReadOnlyBrowser._route_request(
            route,
            FakeRequest(url="https://example.invalid/auth?password=must-not-be-logged"),
            policy,
            {"manual_auth_active": True},
            blocked,
        )
        self.assertEqual(route.action, "continue")
        self.assertEqual(blocked, [])
        source = inspect.getsource(PlaywrightReadOnlyBrowser._route_request)
        self.assertNotIn("post_data", source)
        self.assertNotIn("headers", source)
        browser_source = inspect.getsource(PlaywrightReadOnlyBrowser)
        self.assertIn('if request_state.get("manual_auth_active") or len(public_network)', browser_source)

    def test_manual_auth_does_not_enable_application_mutations(self):
        policy = BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True)
        self.assertFalse(policy.allow_form_input)
        self.assertFalse(policy.allow_file_upload)
        self.assertFalse(policy.allow_submit)

    def test_manual_auth_handoff_resumes_same_session_and_extracts_form(self):
        page = FakeWorkdayPage(fake_workday_payloads())
        context = FakeContext(page)
        browser = FakeBrowser(context)
        manager = FakePlaywrightManager(browser)

        def confirm(_prompt):
            page.state = 2
            return ""

        backend = PlaywrightReadOnlyBrowser(
            manual_auth_confirmation=confirm,
            manual_auth_notice=lambda _message: None,
        )
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.TimeoutError = FakePlaywrightTimeout
        sync_api.sync_playwright = lambda: manager
        package = types.ModuleType("playwright")
        package.sync_api = sync_api
        with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}):
            rendered = backend.inspect(
                page.url,
                BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True),
            )

        self.assertEqual(rendered.status, "DETECTED")
        self.assertEqual([item.field_id for item in rendered.fields], ["email"])
        self.assertEqual(page.clicked, ["Apply Manually"])
        self.assertTrue(rendered.metadata["manual_auth_handoff_started"])
        self.assertTrue(rendered.metadata["manual_auth_resumed"])
        self.assertTrue(rendered.metadata["manual_auth_firewall_rearmed"])
        self.assertTrue(page.closed and context.closed and browser.closed)

    def test_manual_auth_disabled_does_not_choose_or_handoff(self):
        page = FakeWorkdayPage(fake_workday_payloads())
        context = FakeContext(page)
        browser = FakeBrowser(context)
        manager = FakePlaywrightManager(browser)
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.TimeoutError = FakePlaywrightTimeout
        sync_api.sync_playwright = lambda: manager
        package = types.ModuleType("playwright")
        package.sync_api = sync_api
        with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}):
            rendered = PlaywrightReadOnlyBrowser().inspect(page.url, BrowserSessionPolicy())
        self.assertEqual(page.clicked, [])
        self.assertFalse(rendered.metadata["manual_auth_requested"])

    def test_login_or_captcha_still_present_after_handoff_is_reported_without_loop(self):
        for blocker, expected in (("login", "LOGIN_REQUIRED"), ("captcha", "BLOCKED_BY_ANTI_BOT")):
            payloads = fake_workday_payloads()
            if blocker == "captcha":
                payloads[1]["login_wall"] = False
                payloads[1]["password_input"] = False
                payloads[1]["headings"] = ["Security Challenge"]
                payloads[1]["anti_bot"] = True
            page = FakeWorkdayPage(payloads)
            context = FakeContext(page)
            browser = FakeBrowser(context)
            manager = FakePlaywrightManager(browser)
            sync_api = types.ModuleType("playwright.sync_api")
            sync_api.TimeoutError = FakePlaywrightTimeout
            sync_api.sync_playwright = lambda: manager
            package = types.ModuleType("playwright")
            package.sync_api = sync_api
            backend = PlaywrightReadOnlyBrowser(
                manual_auth_confirmation=lambda _prompt: "",
                manual_auth_notice=lambda _message: None,
            )
            with patch.dict(sys.modules, {"playwright": package, "playwright.sync_api": sync_api}):
                rendered = backend.inspect(
                    page.url,
                    BrowserSessionPolicy(headless=False, allow_manual_auth_handoff=True),
                )
            with self.subTest(blocker=blocker):
                self.assertEqual(rendered.status, expected)
                self.assertTrue(rendered.metadata["manual_auth_firewall_rearmed"])

    def test_manual_auth_eof_cancels_safely(self):
        backend = PlaywrightReadOnlyBrowser(
            manual_auth_confirmation=lambda _prompt: (_ for _ in ()).throw(EOFError()),
            manual_auth_notice=lambda _message: None,
        )
        state = {"manual_auth_active": False}
        result = backend._perform_manual_auth_handoff(object(), state)
        self.assertTrue(result["manual_auth_cancelled"])
        self.assertTrue(result["manual_auth_firewall_rearmed"])
        self.assertFalse(state["manual_auth_active"])

    def test_terminal_auth_prompt_does_not_contaminate_json_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("builtins.input", return_value=""), redirect_stdout(stdout), redirect_stderr(stderr):
            response = PlaywrightReadOnlyBrowser._terminal_confirmation("Authenticate, then continue")
        self.assertEqual(response, "")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Authenticate", stderr.getvalue())

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

    def test_blocked_request_diagnostics_serialize_without_query_data(self):
        snapshot = PlaywrightReadOnlyBrowser._snapshot(
            requested_url="https://example.invalid/jobs/123",
            final_url="https://example.invalid/jobs/123",
            page_title="Fictional job",
            payload={"fields": [], "dom_markers": ["workday"]},
            blocked_requests=[{"method": "POST", "url": "https://example.invalid/session"}],
            public_network=[],
            navigation_followed=False,
            error_message="",
            session_metadata={"manual_auth_firewall_rearmed": True},
        )
        value = json.dumps(snapshot.to_dict())
        self.assertIn('"blocked_requests"', value)
        self.assertNotIn("secret=", value)

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

    def test_workday_radio_group_inherits_visible_required_marker(self):
        raw = [
            {
                "field_id": f"previous_worker_{index}",
                "section_id": "my_information",
                "section_label": "My Information",
                "label": "",
                "accessible_name": option,
                "dom_type": "radio",
                "required": False,
                "metadata": {
                    "control_label": option,
                    "group_heading": "Have you previously been employed by Fictional Transit?*",
                    "choice_group_id": "candidateIsPreviousWorker",
                },
            }
            for index, option in enumerate(("Yes", "No"), start=1)
        ]
        field = PlaywrightReadOnlyBrowser._merge_choice_groups(raw)[0]
        self.assertTrue(field.required)
        self.assertEqual(field.field_id, "candidateIsPreviousWorker")
        self.assertTrue(field.metadata["required_from_visible_group_label"])

    def test_workday_searchable_chooser_normalizes_to_select(self):
        raw = {
            "field_id": "fictional_source",
            "section_id": "source",
            "section_label": "Source",
            "label": "How Did You Hear About Us?*",
            "dom_type": "unknown",
            "required": True,
            "metadata": {"role": "combobox", "tag_name": "div"},
        }
        field = PlaywrightReadOnlyBrowser._merge_choice_groups([raw])[0]
        self.assertEqual(field.dom_type, "select")

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

    def test_playwright_backend_contains_no_input_upload_or_submission_calls(self):
        source = inspect.getsource(PlaywrightReadOnlyBrowser)
        for forbidden in (".fill(", ".type(", ".check(", "set_input_files", "select_option"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertEqual(source.count(".click("), 1)
        self.assertIn('re.compile(r"^Apply Manually$"', source)

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

    def test_cli_rejects_manual_auth_without_headed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "inspect-application-form",
                "--url", self.snapshot.requested_url,
                "--manual-auth",
            ])
        self.assertEqual(code, 2)
        self.assertIn("--headed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
