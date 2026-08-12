"""Playwright backend with an enforced read-only, non-persistent context."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .ats_detection import detect_rendered_ats
from .base import (
    BrowserFieldSnapshot,
    BrowserRenderedForm,
    BrowserSessionPolicy,
    validate_public_browser_url,
)


class BrowserDependencyError(RuntimeError):
    pass


_DOM_EXTRACTION_SCRIPT = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  };
  const textByIds = (ids) => clean(String(ids || '').split(/\s+/).map((id) => {
    const node = document.getElementById(id); return node ? node.textContent : '';
  }).join(' '));
  const directLabelFor = (el) => {
    const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const nested = el.closest('label');
    return clean(
      (explicit && explicit.textContent) ||
      (nested && nested.textContent) ||
      el.getAttribute('aria-label') ||
      textByIds(el.getAttribute('aria-labelledby')) ||
      el.getAttribute('placeholder')
    );
  };
  const helpFor = (el) => textByIds(el.getAttribute('aria-describedby'));
  const groupContextFor = (el) => {
    const fieldset = el.closest('fieldset');
    const semanticGroup = el.closest(
      'fieldset, [role="radiogroup"], [role="group"], [data-question], '
      + '[data-automation-id*="question" i], [class*="question" i], '
      + '[class*="application-field" i], [class*="custom-field" i], [class*="card" i]'
    );
    const group = semanticGroup && semanticGroup !== el ? semanticGroup : null;
    const legend = fieldset && fieldset.querySelector(':scope > legend');
    const heading = group && group.querySelector(
      ':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > [role="heading"], '
      + ':scope > [class*="question-title" i], :scope > [class*="field-title" i], '
      + ':scope > [class*="question-label" i]'
    );
    const wrapperLabel = group && Array.from(group.children).find((node) =>
      node.tagName === 'LABEL' && !node.contains(el)
    );
    return {
      fieldset_legend: clean(legend && legend.textContent),
      group_accessible_name: clean(group && (
        group.getAttribute('aria-label') || textByIds(group.getAttribute('aria-labelledby'))
      )),
      group_heading: clean(heading && heading.textContent),
      wrapper_label: clean(wrapperLabel && wrapperLabel.textContent),
      group_description: clean(group && textByIds(group.getAttribute('aria-describedby'))),
      choice_group_id: clean(
        el.getAttribute('name') ||
        (group && (group.id || group.getAttribute('data-automation-id') || group.getAttribute('aria-labelledby')))
      )
    };
  };
  const sectionFor = (el) => {
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector(':scope > legend');
      return {id: fieldset.id || fieldset.getAttribute('data-section') || 'application',
              label: clean(legend && legend.textContent) || 'Application'};
    }
    const group = el.closest('[role="group"], [data-section], section');
    if (group) {
      const labelled = textByIds(group.getAttribute('aria-labelledby'));
      const heading = group.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > [role="heading"]');
      return {id: group.id || group.getAttribute('data-section') || 'application',
              label: labelled || clean(heading && heading.textContent) || 'Application'};
    }
    let prior = el.previousElementSibling;
    while (prior) {
      if (/^H[1-4]$/.test(prior.tagName) || prior.getAttribute('role') === 'heading') {
        return {id: clean(prior.id) || 'application', label: clean(prior.textContent) || 'Application'};
      }
      prior = prior.previousElementSibling;
    }
    return {id: 'application', label: 'Application'};
  };
  const locatorFor = (el, index) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const automation = el.getAttribute('data-automation-id');
    if (automation) return `[data-automation-id="${automation.replace(/"/g, '\\"')}"]`;
    const name = el.getAttribute('name');
    if (name) return `${el.tagName.toLowerCase()}[name="${name.replace(/"/g, '\\"')}"]`;
    const aria = el.getAttribute('aria-label');
    if (aria) return `${el.tagName.toLowerCase()}[aria-label="${aria.replace(/"/g, '\\"')}"]`;
    return `${el.tagName.toLowerCase()}:nth-of-type(${index + 1})`;
  };
  const optionsFor = (el) => {
    if (el.tagName === 'SELECT') return Array.from(el.options).map((o) => clean(o.textContent)).filter(Boolean);
    const controls = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
    let root = controls ? document.getElementById(controls) : null;
    if (!root) root = el.closest('[role="group"]') || el.parentElement;
    return root ? Array.from(root.querySelectorAll('[role="option"], [role="radio"], [role="checkbox"]'))
      .map((o) => clean(o.getAttribute('aria-label') || o.textContent)).filter(Boolean) : [];
  };
  const repeatFor = (el, section) => {
    const holder = el.closest('[data-repeat-group], [data-automation-id*="education" i], [data-automation-id*="experience" i], [data-automation-id*="employment" i], [data-automation-id*="certif" i]');
    const evidence = clean((holder && (holder.getAttribute('data-repeat-group') || holder.getAttribute('data-automation-id'))) || section.label).toLowerCase();
    let group = '';
    if (/education|school/.test(evidence)) group = 'education';
    else if (/employment|work history|experience/.test(evidence)) group = 'employment';
    else if (/certif|credential/.test(evidence)) group = 'certifications';
    const rawIndex = holder && (holder.getAttribute('data-repeat-index') || holder.getAttribute('data-index'));
    const repeatIndex = rawIndex !== null && rawIndex !== '' && !Number.isNaN(Number(rawIndex)) ? Number(rawIndex) : null;
    return {group, index: repeatIndex};
  };

  const nodes = Array.from(document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]), textarea, select, [role="combobox"], [role="radio"], [role="checkbox"]'
  ));
  const rawFields = nodes.map((el, index) => {
    const controlLabel = directLabelFor(el);
    const groupContext = groupContextFor(el);
    const section = sectionFor(el);
    const repeat = repeatFor(el, section);
    const role = clean(el.getAttribute('role')).toLowerCase();
    const inputType = clean(el.getAttribute('type') || el.tagName).toLowerCase();
    let domType = inputType;
    if (el.tagName === 'TEXTAREA') domType = 'textarea';
    else if (el.tagName === 'SELECT') domType = el.multiple ? 'multiselect' : 'select';
    else if (role === 'combobox') domType = el.getAttribute('aria-multiselectable') === 'true' ? 'multiselect' : 'select';
    else if (role === 'radio') domType = 'radio';
    else if (role === 'checkbox') domType = 'checkbox';
    const fieldId = clean(el.id || el.getAttribute('name') || el.getAttribute('data-automation-id') || `browser_field_${index + 1}`);
    const isChoice = domType === 'radio' || domType === 'checkbox';
    const label = isChoice ? '' : clean(
      controlLabel || groupContext.fieldset_legend || groupContext.group_accessible_name ||
      groupContext.group_heading || groupContext.wrapper_label ||
      el.getAttribute('name') || el.id || fieldId
    );
    const isVisible = visible(el);
    const rules = {};
    ['pattern', 'min', 'max', 'minlength', 'maxlength', 'accept'].forEach((name) => {
      const value = el.getAttribute(name); if (value !== null) rules[name] = value;
    });
    if (el.getAttribute('aria-required') !== null) rules['aria_required'] = el.getAttribute('aria-required');
    return {
      field_id: fieldId,
      section_id: section.id,
      section_label: section.label,
      label,
      accessible_name: clean(el.getAttribute('aria-label') || textByIds(el.getAttribute('aria-labelledby')) || controlLabel),
      dom_type: domType,
      required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
      options: optionsFor(el),
      placeholder: clean(el.getAttribute('placeholder')),
      help_text: helpFor(el),
      visible: isVisible,
      hidden: !isVisible || el.getAttribute('aria-hidden') === 'true',
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      locator: locatorFor(el, index),
      validation_rules: rules,
      repeat_group: repeat.group,
      repeat_index: repeat.index,
      metadata: {
        role,
        tag_name: el.tagName.toLowerCase(),
        control_label: controlLabel,
        control_accessible_name: clean(el.getAttribute('aria-label') || textByIds(el.getAttribute('aria-labelledby'))),
        ...groupContext
      }
    };
  });

  const bodyText = clean(document.body && document.body.innerText).toLowerCase();
  const antiBot = Boolean(document.querySelector('iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], [class*="captcha" i], [id*="captcha" i]')) ||
    /verify you are human|security challenge|unusual traffic/.test(bodyText);
  const passwordInput = Boolean(document.querySelector('input[type="password"]'));
  const accountWall = passwordInput || /^(sign in|log in|create (an )?account)$/i.test(clean(document.querySelector('h1, h2, [role="heading"]')?.textContent));
  const nextControls = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'))
    .filter(visible).map((el) => clean(el.textContent || el.getAttribute('value') || el.getAttribute('aria-label')))
    .filter((text) => /^(next|continue|save and continue|review)$/i.test(text));
  const stepNodes = Array.from(document.querySelectorAll('[aria-current="step"], [role="progressbar"], [class*="step" i] [aria-current]'));
  const currentStep = clean(stepNodes[0]?.textContent || stepNodes[0]?.getAttribute('aria-label'));
  const signatureText = clean(Array.from(document.querySelectorAll('script[src], link[href], meta[name], [data-automation-id]'))
    .slice(0, 100).map((el) => el.getAttribute('src') || el.getAttribute('href') || el.getAttribute('name') || el.getAttribute('data-automation-id')).join(' '));
  return {
    page_title: clean(document.title),
    fields: rawFields,
    anti_bot: antiBot,
    login_wall: accountWall,
    current_step: currentStep,
    step_count: stepNodes.length || null,
    later_steps_unavailable_without_submission: nextControls.length > 0,
    dom_markers: [signatureText],
    apply_href: (() => {
      const links = Array.from(document.querySelectorAll('a[href]')).filter(visible);
      const match = links.find((el) => /^(apply|apply now|apply for this job|i'm interested)$/i.test(clean(el.textContent || el.getAttribute('aria-label'))));
      return match ? match.href : '';
    })()
  };
}
"""


class PlaywrightReadOnlyBrowser:
    """Render public pages without calling any input, upload, or submission API."""

    def __init__(self, *, executable_path: str | None = None):
        self.executable_path = executable_path or os.environ.get("JOBHUNTBOT_PLAYWRIGHT_EXECUTABLE", "")

    def inspect(self, url: str, policy: BrowserSessionPolicy) -> BrowserRenderedForm:
        requested_url = validate_public_browser_url(url)
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserDependencyError(
                "Playwright is not installed. Install the browser extra and Chromium: "
                "pip install -e .[browser] && python -m playwright install chromium"
            ) from exc

        blocked_requests: list[dict[str, str]] = []
        public_network: list[dict[str, Any]] = []
        navigation_followed = False
        final_payload: dict[str, Any] = {}
        final_url = requested_url
        page_title = ""
        error_message = ""

        if self.executable_path and not Path(self.executable_path).is_file():
            raise BrowserDependencyError(f"Configured Chromium executable does not exist: {self.executable_path}")

        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": policy.headless}
            if self.executable_path:
                launch_options["executable_path"] = self.executable_path
            try:
                browser = playwright.chromium.launch(**launch_options)
            except Exception as exc:
                raise BrowserDependencyError(
                    "Chromium could not be launched. Run 'python -m playwright install chromium' "
                    "or set JOBHUNTBOT_PLAYWRIGHT_EXECUTABLE to an existing Playwright-compatible Chromium. "
                    f"Original error: {exc}"
                ) from exc
            context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
                locale="en-US",
            )
            page = context.new_page()
            page.set_default_timeout(policy.timeout_ms)

            def route_request(route: Any, request: Any) -> None:
                method = request.method.upper()
                if method not in policy.allowed_http_methods:
                    blocked_requests.append({"method": method, "url": self._sanitize_url(request.url)})
                    route.abort("blockedbyclient")
                    return
                route.continue_()

            def observe_response(response: Any) -> None:
                if len(public_network) >= 50:
                    return
                request = response.request
                if request.resource_type in {"document", "xhr", "fetch"}:
                    public_network.append(
                        {
                            "method": request.method.upper(),
                            "url": self._sanitize_url(response.url),
                            "status": response.status,
                            "resource_type": request.resource_type,
                        }
                    )

            context.route("**/*", route_request)
            page.on("response", observe_response)
            try:
                page.goto(requested_url, wait_until="domcontentloaded", timeout=policy.timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(policy.timeout_ms, 5_000))
                except PlaywrightTimeoutError:
                    pass
                final_payload = page.evaluate(_DOM_EXTRACTION_SCRIPT)
                if (
                    policy.follow_public_apply_link
                    and policy.max_public_navigations > 1
                    and not final_payload.get("anti_bot")
                    and not final_payload.get("login_wall")
                    and not self._has_application_evidence(final_payload.get("fields", []))
                    and final_payload.get("apply_href")
                ):
                    apply_url = validate_public_browser_url(urljoin(page.url, str(final_payload["apply_href"])))
                    if apply_url != page.url:
                        page.goto(apply_url, wait_until="domcontentloaded", timeout=policy.timeout_ms)
                        try:
                            page.wait_for_load_state("networkidle", timeout=min(policy.timeout_ms, 5_000))
                        except PlaywrightTimeoutError:
                            pass
                        navigation_followed = True
                        final_payload = page.evaluate(_DOM_EXTRACTION_SCRIPT)
                final_url = page.url
                page_title = str(final_payload.get("page_title", ""))
            except Exception as exc:  # browser errors become explainable status, never fallback interaction
                error_message = f"{type(exc).__name__}: {exc}"
                final_url = page.url or requested_url
            finally:
                page.close()
                context.close()
                browser.close()

        return self._snapshot(
            requested_url=requested_url,
            final_url=final_url,
            page_title=page_title,
            payload=final_payload,
            blocked_requests=blocked_requests,
            public_network=public_network,
            navigation_followed=navigation_followed,
            error_message=error_message,
        )

    @staticmethod
    def _snapshot(
        *,
        requested_url: str,
        final_url: str,
        page_title: str,
        payload: dict[str, Any],
        blocked_requests: list[dict[str, str]],
        public_network: list[dict[str, Any]],
        navigation_followed: bool,
        error_message: str,
    ) -> BrowserRenderedForm:
        fields = PlaywrightReadOnlyBrowser._merge_choice_groups(payload.get("fields", []))
        login_wall = bool(payload.get("login_wall"))
        anti_bot = bool(payload.get("anti_bot"))
        blockers: list[str] = []
        if error_message:
            status = "ERROR"
            blockers.append(error_message)
        elif anti_bot:
            status = "BLOCKED_BY_ANTI_BOT"
            blockers.append("A CAPTCHA or anti-bot challenge was detected; inspection stopped without bypass.")
        elif login_wall:
            status = "LOGIN_REQUIRED"
            blockers.append("An account or login wall was detected; no credentials were used.")
        elif fields and PlaywrightReadOnlyBrowser._has_application_evidence([item.to_dict() for item in fields]):
            status = "PARTIAL" if payload.get("later_steps_unavailable_without_submission") else "DETECTED"
        elif fields:
            status = "NO_FORM_DETECTED"
            blockers.append("Page controls were present, but no application-form evidence was detected.")
        elif blocked_requests:
            status = "DYNAMIC_BUT_INACCESSIBLE"
            blockers.append("Page rendering required blocked non-read-only requests; no mutation was permitted.")
        else:
            status = "NO_FORM_DETECTED"
            blockers.append("No browser-rendered application form fields were detected.")
        return BrowserRenderedForm(
            requested_url=requested_url,
            final_url=final_url,
            page_title=page_title,
            ats_hint=detect_rendered_ats(final_url, dom_markers=payload.get("dom_markers", [])),
            status=status,
            fields=fields,
            blockers=blockers,
            current_step=str(payload.get("current_step", "")),
            step_count=payload.get("step_count"),
            later_steps_unavailable_without_submission=bool(payload.get("later_steps_unavailable_without_submission")),
            login_wall=login_wall,
            anti_bot=anti_bot,
            apply_navigation_followed=navigation_followed,
            blocked_request_count=len(blocked_requests),
            public_network_metadata=public_network,
            metadata={
                "dom_markers": payload.get("dom_markers", []),
                "blocked_request_methods": sorted({item["method"] for item in blocked_requests}),
                "candidate_data_read": False,
                "candidate_data_typed": False,
                "file_uploads": 0,
                "form_submissions": 0,
            },
        )

    @staticmethod
    def _merge_choice_groups(raw_fields: list[dict[str, Any]]) -> list[BrowserFieldSnapshot]:
        merged: dict[tuple[str, str, str], BrowserFieldSnapshot] = {}
        for raw in raw_fields:
            contextual = PlaywrightReadOnlyBrowser._apply_question_context(raw)
            item = BrowserFieldSnapshot.from_dict(contextual)
            group_id = str(item.metadata.get("choice_group_id", "")).strip()
            key = (item.section_id, group_id or item.field_id, item.dom_type)
            if item.dom_type in {"radio", "checkbox"} and key in merged:
                existing = merged[key]
                option = str(item.metadata.get("control_label", "")).strip()
                existing.options = [
                    value for value in dict.fromkeys([*existing.options, option, *item.options]) if value
                ]
                existing.required = existing.required or item.required
                existing.visible = existing.visible or item.visible
                existing.hidden = existing.hidden and item.hidden
                continue
            if item.dom_type in {"radio", "checkbox"}:
                if group_id:
                    item.field_id = group_id
                option = str(item.metadata.get("control_label", "")).strip()
                item.options = list(dict.fromkeys([option, *item.options])) if option else item.options
            merged[key] = item
        return list(merged.values())

    @staticmethod
    def _apply_question_context(raw: dict[str, Any]) -> dict[str, Any]:
        """Promote structural group text to the question without using answer options."""

        value = dict(raw)
        metadata = dict(value.get("metadata", {}))
        value["metadata"] = metadata
        control_label = str(metadata.get("control_label", value.get("label", ""))).strip()
        option_values = {
            str(item).strip().casefold()
            for item in value.get("options", [])
            if str(item).strip()
        }
        if control_label:
            option_values.add(control_label.casefold())
        context = ""
        context_source = ""
        for key in (
            "fieldset_legend",
            "group_accessible_name",
            "group_heading",
            "wrapper_label",
            "group_description",
        ):
            candidate = " ".join(str(metadata.get(key, "")).split())
            if candidate and candidate.casefold() not in option_values:
                context = candidate
                context_source = key
                break
        if context:
            metadata["question_context"] = context
            metadata["question_context_source"] = context_source

        dom_type = str(value.get("dom_type", "")).casefold()
        if dom_type in {"radio", "checkbox"}:
            metadata.setdefault("control_accessible_name", value.get("accessible_name", ""))
            value["label"] = context
            value["accessible_name"] = context
        elif context and not str(metadata.get("control_label", "")).strip():
            value["label"] = context
            value["accessible_name"] = value.get("accessible_name") or context
        return value

    @staticmethod
    def _has_application_evidence(fields: list[dict[str, Any]]) -> bool:
        evidence = re.compile(
            r"\b(first name|last name|full name|email|phone|resume|cv|cover letter|linkedin|"
            r"authori[sz]|sponsor|gender|race|veteran|disability|consent|signature)\b",
            re.IGNORECASE,
        )
        return any(
            str(item.get("dom_type", "")).casefold() == "file"
            or evidence.search(str(item.get("label") or item.get("accessible_name") or ""))
            for item in fields
            if not item.get("disabled", False)
        )

    @staticmethod
    def _sanitize_url(url: str) -> str:
        parsed = urlsplit(str(url or ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
