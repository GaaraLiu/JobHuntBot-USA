from __future__ import annotations

import copy
import inspect
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from jobhuntbot.application_forms.models import (
    ApplicationField, ApplicationForm, ApplicationMappingPlan, FieldMappingPlan,
    FormSection,
)
from jobhuntbot.application_queue import ApplicationQueueStore
from jobhuntbot.browser_forms import BrowserFieldSnapshot, BrowserRenderedForm
from jobhuntbot.cli import main
from jobhuntbot.controlled_fill import ControlledFillPlanner
from jobhuntbot.reviewed_submission import (
    ApprovalService, ConfirmationDetector, ReviewPackageBuilder,
    ScopedSubmissionFirewall, StepTransitionGuard, SubmissionApproval,
    SubmissionContext, SubmissionExecutionPolicy, SubmissionExecutionService,
    SubmissionHistoryStore, SubmissionQueueStatusManager, SubmissionResult,
    SubmitControlDetector,
)
from jobhuntbot.reviewed_submission.playwright_submission import (
    PlaywrightReviewedSubmissionBrowser, _REVIEWED_SUBMISSION_GUARD,
)
from jobhuntbot.reviewed_submission.documents import validate_pdf_document


def confirmed(value):
    return {"value": value, "status": "confirmed", "source": "fictional test"}


class FakeSubmissionBackend:
    def __init__(self, status="SUBMITTED_CONFIRMED"):
        self.status = status
        self.calls = 0

    def execute(self, context, approval, policy):
        self.calls += 1
        confirmed_status = self.status == "SUBMITTED_CONFIRMED"
        return SubmissionResult(
            application_id=context.review.application_id,
            job_id=context.review.job_id,
            company=context.review.job["company"],
            title=context.review.job["title"],
            ats=context.mapping.ats,
            started_at="2026-01-01T00:00:00+00:00",
            submitted_at="2026-01-01T00:00:01+00:00",
            submission_attempted=True,
            submission_completed=True,
            confirmation_detected=confirmed_status,
            confirmation_text_summary="Fictional confirmation." if confirmed_status else "",
            confirmation_reference_id="TEST-1234" if confirmed_status else "",
            final_url="https://example.invalid/confirmation",
            resume_uploaded=True,
            cover_letter_uploaded=False,
            manual_interventions=[],
            unresolved_fields=[],
            safety_flags=["FICTIONAL_OFFLINE_BACKEND"],
            status=self.status,
        )


class FakeUploadLocator:
    def __init__(self):
        self.selected = ""

    def count(self):
        return 1

    def set_input_files(self, path):
        self.selected = path


class FakeUploadPage:
    def __init__(self):
        self.control = FakeUploadLocator()

    def locator(self, path):
        return self.control


class ReviewedSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "my-materials" / "application"
        self.root.mkdir(parents=True)
        self.resume = self.root / "fictional_resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4\n% fictional resume\n")

    def tearDown(self):
        self.temp.cleanup()

    def artifacts(
        self, *, legal=False, demographic=False, unresolved=False,
        cover_required=False, captcha=False, login=False,
    ):
        package = {
            "application_id": "application_fictional",
            "job_snapshot": {
                "job_id": "job_fictional", "company": "Fictional Company",
                "title": "Fictional Analyst", "location": "Example City, EX",
                "salary": {"minimum": 70000, "maximum": 90000},
            },
            "selected_resume": {
                "resume_id": "fictional_data", "path": str(self.resume),
                "verified_exists": True, "substitution_allowed": False,
            },
            "candidate_facts": {
                "identity.email": confirmed("avery@example.invalid"),
            },
            "prepared_answers": [
                {"question_id": "auth", "canonical_id": "work_authorization_us",
                 "value": True, "safety_class": "STATIC_CONFIRMED",
                 "provenance": ["fictional test"], "allowed_autofill": True},
                {"question_id": "sponsor-now", "canonical_id": "sponsorship_now",
                 "value": False, "safety_class": "STATIC_CONFIRMED",
                 "provenance": ["fictional test"], "allowed_autofill": True},
                {"question_id": "sponsor-future", "canonical_id": "sponsorship_future",
                 "value": False, "safety_class": "STATIC_CONFIRMED",
                 "provenance": ["fictional test"], "allowed_autofill": True},
            ],
            "job_dependent_answers": {
                "salary": {"proposed_answer": "Negotiable", "requires_confirmation": False},
            },
            "unresolved_questions": [],
            "optional_documents": [],
            "cover_letter_status": "required" if cover_required else "not_required",
            "package_readiness": "READY",
            "blockers": [],
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        specs = [
            {
                "id": "email", "label": "Email", "form_type": "email", "browser_type": "email",
                "required": True, "canonical": "contact_email", "action": "AUTO_READY_FUTURE",
                "safety": "STATIC_CONFIRMED", "source": "identity.email",
            },
            {
                "id": "resume", "label": "Resume", "form_type": "resume", "browser_type": "file",
                "required": True, "canonical": "resume_upload", "action": "MANUAL_ONLY",
                "safety": "MANUAL_ONLY", "source": "", "rules": {"accept": ".pdf", "max_size_bytes": 1000000},
            },
        ]
        if legal:
            specs.append({
                "id": "legal", "label": "Privacy acknowledgment", "form_type": "consent",
                "browser_type": "checkbox", "required": True,
                "canonical": "privacy_acknowledgment", "action": "MANUAL_ONLY",
                "safety": "MANUAL_ONLY", "source": "",
            })
        if demographic:
            specs.append({
                "id": "gender", "label": "Gender", "form_type": "select", "browser_type": "select",
                "required": False, "canonical": "gender", "action": "OPTIONAL_SKIP",
                "safety": "OPTIONAL_PREFER_NOT_TO_ANSWER", "source": "", "options": ["Prefer not to answer"],
            })
        if unresolved:
            specs.append({
                "id": "custom", "label": "Why this company?", "form_type": "textarea",
                "browser_type": "textarea", "required": True, "canonical": "",
                "action": "UNRESOLVED", "safety": "UNKNOWN", "source": "",
                "confidence": "UNMAPPED",
            })
            package["unresolved_questions"] = [{
                "question_id": "custom", "canonical_id": "", "required": True,
                "resolved": False, "reason": "Fictional custom answer required.",
            }]
            package["package_readiness"] = "NEEDS_USER_INPUT"
        if cover_required:
            specs.append({
                "id": "cover", "label": "Cover Letter", "form_type": "cover_letter",
                "browser_type": "file", "required": True, "canonical": "cover_letter_upload",
                "action": "MANUAL_ONLY", "safety": "MANUAL_ONLY", "source": "",
                "rules": {"accept": "application/pdf"},
            })

        form_fields, browser_fields, mapping_fields = [], [], []
        for spec in specs:
            locator = f"#{spec['id']}"
            form_fields.append(ApplicationField(
                field_id=spec["id"], section="application", label=spec["label"],
                normalized_label=spec["label"].casefold(), field_type=spec["form_type"],
                required=spec["required"], options=list(spec.get("options", [])),
                validation_rules=dict(spec.get("rules", {})), source_path=locator,
            ))
            browser_fields.append(BrowserFieldSnapshot(
                field_id=spec["id"], section_id="application", section_label="Application",
                label=spec["label"], dom_type=spec["browser_type"], required=spec["required"],
                options=list(spec.get("options", [])), visible=True, disabled=False, locator=locator,
            ))
            mapping_fields.append(FieldMappingPlan(
                field_id=spec["id"], label=spec["label"], required=spec["required"],
                canonical_question_id=spec["canonical"],
                mapping_confidence=spec.get("confidence", "HIGH"), safety_class=spec["safety"],
                answer_status="confirmed", action=spec["action"], reason="fictional mapping",
                source_reference=spec["source"],
            ))
        form = ApplicationForm(
            source="fictional", ats="lever", application_url="https://example.invalid/apply",
            job_id="job_fictional", company="Fictional Company", title="Fictional Analyst",
            detected_at="2026-01-01T00:00:00+00:00",
            sections=[FormSection("application", "Application", form_fields)],
        )
        form.compute_fingerprint()
        mapping = ApplicationMappingPlan(
            application_id="application_fictional", job_id="job_fictional",
            form_fingerprint=form.fingerprint, ats="lever",
            generated_at="2026-01-01T00:00:00+00:00", fields=mapping_fields,
            total_fields=len(mapping_fields), mapped_fields=sum(bool(x.canonical_question_id) for x in mapping_fields),
            required_fields=sum(x.required for x in mapping_fields), required_unresolved=int(unresolved),
            manual_only=sum(x.action == "MANUAL_ONLY" for x in mapping_fields),
            potential_future_autofill=sum(x.action == "AUTO_READY_FUTURE" for x in mapping_fields),
            user_confirmation=0, optional_skip=sum(x.action == "OPTIONAL_SKIP" for x in mapping_fields),
            package_readiness="NEEDS_USER_INPUT" if unresolved else "READY",
        )
        status = "BLOCKED_BY_ANTI_BOT" if captcha else "LOGIN_REQUIRED" if login else "DETECTED"
        rendered = BrowserRenderedForm(
            requested_url=form.application_url, final_url=form.application_url,
            page_title=form.title, ats_hint="lever", status=status, fields=browser_fields,
            anti_bot=captcha, login_wall=login,
        )
        plan = ControlledFillPlanner().build(package, mapping, form, rendered)
        review = ReviewPackageBuilder().build(
            package, form, mapping, plan, rendered,
            queue_record={"fit_score": 82.0},
        )
        return package, form, mapping, rendered, plan, review

    def context(self, **kwargs):
        package, form, mapping, rendered, plan, review = self.artifacts(**kwargs)
        return SubmissionContext(package, form, mapping, plan, rendered, review), package, review

    def approval(self, context, package, review, *, for_submission=True, manual=None):
        return ApprovalService().create(
            review, package, context.form, for_submission=for_submission,
            confirmed_reviewed=True, resolved_manual_item_ids=set(manual or []),
        )

    def test_review_package_contains_job_answers_documents_and_score(self):
        _, _, _, _, _, review = self.artifacts()
        self.assertEqual(review.job["company"], "Fictional Company")
        self.assertEqual(review.job["score"], 82.0)
        self.assertEqual(review.documents[0].status, "READY")
        self.assertEqual(review.unresolved_required_count, 0)

    def test_sensitive_answer_uses_safe_description_not_value(self):
        _, _, _, _, _, review = self.artifacts()
        encoded = json.dumps(review.to_dict())
        self.assertNotIn("avery@example.invalid", encoded)
        self.assertIn("Confirmed private value", encoded)

    def test_approval_requires_explicit_review_confirmation(self):
        context, package, review = self.context()
        with self.assertRaises(ValueError):
            ApprovalService().create(review, package, context.form, for_submission=False, confirmed_reviewed=False)

    def test_approval_is_bound_to_application_id(self):
        context, package, review = self.context()
        package["application_id"] = "different"
        with self.assertRaises(ValueError):
            self.approval(context, package, review)

    def test_approval_expires_after_form_fingerprint_change(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        changed = copy.deepcopy(context.form)
        changed.sections[0].fields[0].required = False
        changed.compute_fingerprint()
        expired = ApprovalService().validate(approval, review, package, changed)
        self.assertEqual(expired.approval_status, "EXPIRED")
        self.assertIn("form fingerprint changed", expired.invalidation_reasons)

    def test_approval_expires_after_package_change(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        package["updated_at"] = "2026-01-02T00:00:00+00:00"
        expired = ApprovalService().validate(approval, review, package, context.form)
        self.assertEqual(expired.approval_status, "EXPIRED")
        self.assertIn("application package version changed", expired.invalidation_reasons)

    def test_exact_resume_file_is_hashed_and_approved(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        self.assertEqual(approval.selected_resume["resume_id"], "fictional_data")
        self.assertEqual(len(approval.selected_resume["sha256"]), 64)
        self.assertTrue(approval.approval_scope.allow_resume_upload)

    def test_resume_content_change_expires_approval(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        self.resume.write_bytes(b"%PDF-1.4\n% changed fictional resume\n")
        fresh = ReviewPackageBuilder().build(package, context.form, context.mapping, context.fill_plan, context.rendered)
        expired = ApprovalService().validate(approval, fresh, package, context.form)
        self.assertEqual(expired.approval_status, "EXPIRED")
        self.assertIn("selected resume changed", expired.invalidation_reasons)

    def test_resume_mismatch_in_package_expires_approval(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        package["selected_resume"]["resume_id"] = "different_route"
        expired = ApprovalService().validate(approval, review, package, context.form)
        self.assertEqual(expired.approval_status, "EXPIRED")

    def test_unverified_package_resume_blocks_required_upload(self):
        package, form, mapping, rendered, plan, _ = self.artifacts()
        package["selected_resume"]["verified_exists"] = False
        review = ReviewPackageBuilder().build(package, form, mapping, plan, rendered)
        self.assertTrue(review.blockers)
        with self.assertRaises(ValueError):
            ApprovalService().create(
                review, package, form, for_submission=True, confirmed_reviewed=True
            )

    def test_offline_upload_mechanics_select_exact_reviewed_resume(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        page = FakeUploadPage()
        uploaded = PlaywrightReviewedSubmissionBrowser._upload_document(
            page, context, approval, "resume_upload", "resume"
        )
        self.assertTrue(uploaded)
        self.assertEqual(page.control.selected, str(self.resume.resolve()))

    def test_upload_rechecks_file_hash_immediately_before_selection(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        self.resume.write_bytes(b"%PDF-1.4\n% modified after approval\n")
        with self.assertRaises(RuntimeError):
            PlaywrightReviewedSubmissionBrowser._upload_document(
                FakeUploadPage(), context, approval, "resume_upload", "resume"
            )

    def test_declared_file_size_limit_produces_file_review_required(self):
        document = validate_pdf_document(
            document_type="resume", document_id="fictional_data",
            configured_path=str(self.resume), required=True, approved_in_package=True,
            constraints={"accept": ".pdf", "max_size_bytes": 5},
        )
        self.assertEqual(document.status, "FILE_REVIEW_REQUIRED")

    def test_required_cover_letter_missing_blocks_submission_approval(self):
        context, package, review = self.context(cover_required=True)
        self.assertTrue(any("cover_letter" in item for item in review.blockers))
        with self.assertRaises(ValueError):
            self.approval(context, package, review)

    def test_captcha_pauses_before_backend(self):
        context, package, review = self.context(captcha=True)
        approval = self.approval(context, package, review)
        backend = FakeSubmissionBackend()
        result = SubmissionExecutionService(backend).run(context, approval, SubmissionExecutionPolicy(submit=True))
        self.assertEqual(result.status, "PAUSED_FOR_CAPTCHA")
        self.assertEqual(backend.calls, 0)

    def test_login_pauses_before_backend(self):
        context, package, review = self.context(login=True)
        approval = self.approval(context, package, review)
        backend = FakeSubmissionBackend()
        result = SubmissionExecutionService(backend).run(context, approval, SubmissionExecutionPolicy(submit=True))
        self.assertEqual(result.status, "PAUSED_FOR_LOGIN")
        self.assertEqual(backend.calls, 0)

    def test_legal_consent_requires_individual_resolution(self):
        context, package, review = self.context(legal=True)
        with self.assertRaises(ValueError):
            self.approval(context, package, review)
        approval = self.approval(context, package, review, manual=["legal"])
        self.assertIn("legal", approval.approval_scope.resolved_manual_item_ids)

    def test_unresolved_required_answer_blocks_submission_approval(self):
        context, package, review = self.context(unresolved=True)
        self.assertGreater(review.unresolved_required_count, 0)
        with self.assertRaises(ValueError):
            self.approval(context, package, review)

    def test_optional_demographic_is_skipped_without_blocking(self):
        _, _, _, _, plan, review = self.artifacts(demographic=True)
        gender = next(item for item in plan.fields if item.canonical_id == "gender")
        self.assertEqual(gender.action, "SKIP_OPTIONAL")
        self.assertEqual(review.unresolved_required_count, 0)

    def test_firewall_blocks_mutations_before_approval(self):
        firewall = ScopedSubmissionFirewall()
        self.assertTrue(firewall.allows("GET", "https://example.invalid/apply", application_id="application_fictional"))
        self.assertFalse(firewall.allows("POST", "https://example.invalid/apply", application_id="application_fictional"))

    def test_firewall_selectively_opens_for_exact_origin_and_application(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        firewall = ScopedSubmissionFirewall()
        firewall.arm(approval)
        self.assertTrue(firewall.allows("POST", "https://example.invalid/apply", application_id="application_fictional"))
        self.assertFalse(firewall.allows("POST", "https://other.invalid/apply", application_id="application_fictional"))
        self.assertFalse(firewall.allows("POST", "https://example.invalid/apply", application_id="different"))
        self.assertFalse(firewall.allows("DELETE", "https://example.invalid/apply", application_id="application_fictional"))

    def test_firewall_can_restrict_mutations_to_reviewed_endpoint_paths(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        firewall = ScopedSubmissionFirewall(allowed_mutation_paths={"/apply"})
        firewall.arm(approval)
        self.assertTrue(firewall.allows("POST", "https://example.invalid/apply", application_id="application_fictional"))
        self.assertFalse(firewall.allows("POST", "https://example.invalid/unreviewed", application_id="application_fictional"))

    def test_non_final_next_is_distinct_from_submit(self):
        self.assertEqual(SubmitControlDetector.detect(["Continue"]).status, "NON_FINAL_NEXT")
        self.assertEqual(SubmitControlDetector.detect(["Submit Application"]).status, "FINAL_SUBMIT")

    def test_new_question_after_step_forces_review(self):
        before = BrowserRenderedForm("https://example.invalid", "https://example.invalid", "", "lever", "DETECTED", fields=[])
        after = copy.deepcopy(before)
        after.fields.append(BrowserFieldSnapshot("new", "app", "Application", "New question", dom_type="text", visible=True, locator="#new"))
        self.assertEqual(StepTransitionGuard.evaluate(before, after), "NEW_QUESTIONS_REVIEW_REQUIRED")

    def test_ambiguous_submit_buttons_are_blocked(self):
        decision = SubmitControlDetector.detect(["Submit", "Apply"])
        self.assertEqual(decision.status, "STOP_FOR_REVIEW")

    def test_execute_defaults_to_dry_run_and_never_calls_backend(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        backend = FakeSubmissionBackend()
        result = SubmissionExecutionService(backend).run(context, approval, SubmissionExecutionPolicy())
        self.assertEqual(result.status, "DRY_RUN_READY")
        self.assertFalse(result.submission_attempted)
        self.assertEqual(backend.calls, 0)

    def test_fill_only_approval_cannot_submit(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review, for_submission=False)
        result = SubmissionExecutionService(FakeSubmissionBackend()).run(context, approval, SubmissionExecutionPolicy(submit=True))
        self.assertEqual(result.status, "SUBMISSION_BLOCKED")

    def test_duplicate_confirmed_submission_is_blocked(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        history = SubmissionHistoryStore(self.root / "submissions" / "history.jsonl")
        history.append("submission_result", application_id=review.application_id, job_id=review.job_id,
                       company=review.job["company"], title=review.job["title"], ats="lever",
                       status="SUBMITTED_CONFIRMED")
        backend = FakeSubmissionBackend()
        result = SubmissionExecutionService(backend, history=history).run(
            context, approval, SubmissionExecutionPolicy(submit=True)
        )
        self.assertEqual(result.status, "SUBMISSION_BLOCKED")
        self.assertEqual(backend.calls, 0)

    def test_duplicate_override_is_explicit(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        history = SubmissionHistoryStore(self.root / "submissions" / "history.jsonl")
        history.append("submission_result", application_id=review.application_id, job_id=review.job_id,
                       company=review.job["company"], title=review.job["title"], ats="lever",
                       status="SUBMITTED_CONFIRMED")
        backend = FakeSubmissionBackend()
        result = SubmissionExecutionService(backend, history=history).run(
            context, approval, SubmissionExecutionPolicy(submit=True, allow_duplicate_submission=True)
        )
        self.assertEqual(result.status, "SUBMITTED_CONFIRMED")
        self.assertEqual(backend.calls, 1)

    def test_positive_confirmation_detection(self):
        detected, summary, reference = ConfirmationDetector.detect(
            "Thank you for applying. Confirmation number TEST-4321"
        )
        self.assertTrue(detected)
        self.assertTrue(summary)
        self.assertEqual(reference, "TEST-4321")

    def test_unconfirmed_submission_result_is_not_confirmed(self):
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        result = SubmissionExecutionService(FakeSubmissionBackend("SUBMITTED_UNCONFIRMED")).run(
            context, approval, SubmissionExecutionPolicy(submit=True)
        )
        self.assertEqual(result.status, "SUBMITTED_UNCONFIRMED")
        self.assertFalse(result.confirmation_detected)

    def test_queue_reaches_submitted_only_after_confirmed_result(self):
        queue_path = self.root / "queue.json"
        store = ApplicationQueueStore(queue_path)
        store.save({"schema_version": 1, "applications": [{
            "application_id": "application_fictional", "job_id": "job_fictional",
            "application_status": "READY_FOR_USER_REVIEW",
        }]})
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        manager = SubmissionQueueStatusManager(store)
        SubmissionExecutionService(FakeSubmissionBackend(), queue=manager).run(
            context, approval, SubmissionExecutionPolicy(submit=True)
        )
        self.assertEqual(store.find("application_fictional")["application_status"], "SUBMITTED")

    def test_unconfirmed_result_keeps_queue_in_progress(self):
        queue_path = self.root / "queue.json"
        store = ApplicationQueueStore(queue_path)
        store.save({"schema_version": 1, "applications": [{
            "application_id": "application_fictional", "job_id": "job_fictional",
            "application_status": "READY_FOR_USER_REVIEW",
        }]})
        context, package, review = self.context()
        approval = self.approval(context, package, review)
        SubmissionExecutionService(
            FakeSubmissionBackend("SUBMITTED_UNCONFIRMED"),
            queue=SubmissionQueueStatusManager(store),
        ).run(context, approval, SubmissionExecutionPolicy(submit=True))
        self.assertEqual(store.find("application_fictional")["application_status"], "IN_PROGRESS")

    def test_existing_submitted_queue_state_is_never_downgraded(self):
        queue_path = self.root / "queue.json"
        store = ApplicationQueueStore(queue_path)
        store.save({"schema_version": 1, "applications": [{
            "application_id": "application_fictional", "job_id": "job_fictional",
            "application_status": "SUBMITTED",
        }]})
        SubmissionQueueStatusManager(store).mark_review_ready("application_fictional")
        self.assertEqual(store.find("application_fictional")["application_status"], "SUBMITTED")

    def test_history_contains_metadata_not_candidate_answers_or_passwords(self):
        history = SubmissionHistoryStore(self.root / "submissions" / "history.jsonl")
        history.append("review_generated", application_id="application_fictional", job_id="job_fictional",
                       company="Fictional Company", title="Fictional Role", ats="lever", status="READY")
        text = history.path.read_text(encoding="utf-8")
        self.assertNotIn("avery@example.invalid", text)
        self.assertNotIn("password", text.casefold())
        self.assertIn('"candidate_values_logged": false', text)

    def test_submission_result_serialization(self):
        context, package, review = self.context()
        result = FakeSubmissionBackend().execute(context, self.approval(context, package, review), SubmissionExecutionPolicy(submit=True))
        value = result.to_dict()
        self.assertEqual(value["status"], "SUBMITTED_CONFIRMED")
        self.assertTrue(value["confirmation_detected"])

    def test_confirmed_status_requires_positive_evidence(self):
        with self.assertRaises(ValueError):
            SubmissionResult(
                application_id="a", job_id="j", company="Fictional", title="Role", ats="lever",
                started_at="", submitted_at="", submission_attempted=True, submission_completed=True,
                confirmation_detected=False, confirmation_text_summary="", confirmation_reference_id="",
                final_url="", resume_uploaded=False, cover_letter_uploaded=False,
                manual_interventions=[], unresolved_fields=[], safety_flags=[], status="SUBMITTED_CONFIRMED",
            )

    def test_browser_submission_guard_blocks_enter_until_explicit_final_gate(self):
        self.assertIn("event.key === 'Enter'", _REVIEWED_SUBMISSION_GUARD)
        self.assertIn("__jobhuntbotApprovedFinalSubmit", _REVIEWED_SUBMISSION_GUARD)
        source = inspect.getsource(PlaywrightReviewedSubmissionBrowser)
        self.assertNotIn('input[type="password"]', source.casefold())
        self.assertIn("set_input_files", source)

    def test_cli_review_approve_and_execute_dry_run_are_separate(self):
        package, form, mapping, rendered, _, _ = self.artifacts()
        paths = {
            "package": self.root / "package.json",
            "form": self.root / "form.json",
            "mapping": self.root / "mapping.json",
            "snapshot": self.root / "snapshot.json",
        }
        values = {"package": package, "form": form.to_dict(), "mapping": mapping.to_dict(), "snapshot": rendered.to_dict()}
        for key, path in paths.items():
            path.write_text(json.dumps(values[key]), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([
                "review-application", "--application-id", "application_fictional",
                "--application-package", str(paths["package"]), "--mapping-plan", str(paths["mapping"]),
                "--application-form", str(paths["form"]), "--browser-snapshot", str(paths["snapshot"]),
                "--output-root", str(self.root), "--queue", str(self.root / "missing_queue.json"), "--json",
            ])
        self.assertEqual(code, 0)
        review_path = self.root / "reviews" / "application_fictional_review.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main([
                "approve-application", "--application-id", "application_fictional",
                "--review-package", str(review_path), "--application-package", str(paths["package"]),
                "--application-form", str(paths["form"]), "--for-submission", "--confirm-reviewed",
                "--output-root", str(self.root), "--json",
            ])
        self.assertEqual(code, 0)
        approval_path = self.root / "approvals" / "application_fictional_approval.json"
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main([
                "execute-application", "--application-id", "application_fictional",
                "--application-package", str(paths["package"]), "--mapping-plan", str(paths["mapping"]),
                "--application-form", str(paths["form"]), "--browser-snapshot", str(paths["snapshot"]),
                "--review-package", str(review_path), "--approval", str(approval_path),
                "--output-root", str(self.root), "--queue", str(self.root / "missing_queue.json"), "--json",
            ])
        self.assertEqual(code, 0)
        value = json.loads(out.getvalue())
        self.assertEqual(value["result"]["status"], "DRY_RUN_READY")
        self.assertFalse(value["result"]["submission_attempted"])


if __name__ == "__main__":
    unittest.main()
