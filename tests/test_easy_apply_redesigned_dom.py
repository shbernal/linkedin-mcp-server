"""Browser-DOM tests for the redesigned Easy Apply dialog.

The unit suite mocks ``page.evaluate``, so the programs behind reading and
answering the form never execute there. These run the whole walk against a
synthetic posting in headless chromium, with the navigation stubbed. Its dialog
imitates the one measured on 2026-09-25 (`docs/easy-apply-markup.md`): a native
modal `<dialog>` opened by a button, labels in four shapes, required marked by
an attribute or only by a `*`, errors as loose paragraphs, checkboxes that take
a click only on their `role` wrapper, an upload that makes its file input on
click and leaves a toast over the footer, and footer buttons with nothing to
find them by. It carries none of LinkedIn's classes, so it is a claim about
which signals are read and written, not about LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import json

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import (
    EasyApplyFormRead,
    EasyApplySubmitRead,
    JobPageReader,
)
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US

#: CI uses ``--dist loadgroup``. Keep every test that launches Chromium on one
#: worker so browser startups cannot compete with the DOM cases' wall-clock
#: timers.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

JOB_URL = "https://www.linkedin.com/jobs/view/123/"


def counter(control_id: str, used: int, limit: int) -> str:
    return (
        f'<div data-testid="text-input-helper-text" id="{control_id}-info"><div>'
        f'<p aria-live="polite" aria-atomic="true">'
        f'<span aria-hidden="true">{used}/{limit}</span>'
        f"<span>{used} of {limit} characters</span></p></div></div>"
    )


CONTACT: dict[str, Any] = {
    "title": "Contact info",
    "required": ["«r3»"],
    "body": (
        '<div componentkey="easyApplyFieldFocus_ea.q::1::FIRST_NAME::firstName.validation">'
        '<label for="«r1»"><div>First name*</div></label>'
        '<div><input required id="«r1»" type="text" aria-describedby="«r1»-info" '
        'value="Santiago "></div>' + counter("«r1»", 9, 100) + "</div>"
        '<div><div><label for="«r2»"><div>Phone country code*</div></label>'
        '<div><select id="«r2»" required><option value="es">Spain (+34)</option>'
        '<option value="fr" selected>France (+33)</option></select></div></div>'
        '<div componentkey="easyApplyFieldFocus_ea.q::3::PHONE_MOBILE::phoneNumber.validation">'
        '<label for="«r3»"><div>Mobile phone number*</div></label>'
        '<div><input required id="«r3»" type="tel" aria-describedby="«r3»-info" '
        'value="%(phone)s"></div></div></div>'
        '<div componentkey="easyApplyFieldFocus_ea.q::4::UNKNOWN::value.validation">'
        '<p>Location (city)</p><div role="status" aria-live="polite"></div>'
        '<div><div><input id="«r4»" data-testid="typeahead-input" autocomplete="off" '
        'aria-autocomplete="list" placeholder="Enter city or location" value="">'
        "</div></div></div>"
    ),
}

RESUME: dict[str, Any] = {
    "title": "",
    "required": [],
    "body": (
        "<div><p>Resume*</p><p>Select or upload a resume</p><div>"
        '<fieldset aria-describedby="error-message-r5" role="radiogroup"><div>'
        '<div id="easyApplyUploadedResumeRef"></div><div>'
        '<div><div><div role="button" tabindex="0"><p>PDF</p><p><span>old.pdf</span>'
        '</p></div></div><div role="radio" tabindex="0" aria-label="old.pdf" '
        'aria-checked="true"><div aria-label="old.pdf">'
        '<input id="«r5»" type="radio" checked name="resume" class="covered">'
        '<label for="«r5»"></label></div></div></div>'
        "</div></div></fieldset>"
        '<div><button type="button" id="upload">Upload resume</button></div>'
        "</div></div>"
        '<div componentkey="easyApplyFieldFocus_ea_validation_p1_0_7">'
        '<div><input id="«r6»" aria-label="Headline" type="text" '
        'aria-describedby="«r6»-info" value=""></div>'
        + counter("«r6»", 0, 127)
        + "</div>"
    ),
}

QUESTIONS: dict[str, Any] = {
    "title": "Additional Questions",
    "required": ["«r7»", "«r8»", "«r9»"],
    "body": (
        "<div><div><p>Notice Period*</p>"
        '<div componentkey="easyApplyFieldFocus_ea_validation_p4_0_1">'
        '<div><input required id="«r7»" aria-label="Notice Period" type="text" '
        'aria-describedby="«r7»-info" value="%(notice)s"></div>'
        + counter("«r7»", 0, 127)
        + "</div></div>"
        "<div><p>Spoken Languages*</p><div>"
        '<textarea id="«r8»" aria-label="Spoken Languages" required rows="4" '
        'aria-describedby="«r8»-info" '
        'componentkey="easyApplyFieldFocus_ea_validation_p4_0_2">%(languages)s</textarea>'
        "</div></div>"
        '<div componentkey="easyApplyFieldFocus_ea_validation_p4_0_3">'
        "<p>Personal data consent*</p>"
        '<fieldset aria-describedby="error-message-r9"><div>'
        '<div role="checkbox" tabindex="0" aria-checked="%(consented)s"><div><div>'
        '<input id="«r9»" tabindex="-1" type="checkbox" class="covered" %(consent)s>'
        '<label for="«r9»"></label></div><p>I consent to the processing of my data.</p>'
        "</div></div></div></fieldset></div></div>"
    ),
}

REVIEW: dict[str, Any] = {
    "title": "",
    "required": [],
    "body": (
        "<div><h3>Review your application</h3>"
        "<p>The employer will also receive a copy of your profile</p></div>"
        "<div><h4>Contact info</h4><div><p>First name</p><p>Santiago</p></div></div>"
        '<div role="checkbox" tabindex="0" aria-label="Follow Acme to stay up to date" '
        'aria-checked="true"><div><div><input id="«r10»" tabindex="-1" type="checkbox" '
        'class="covered" checked><label for="«r10»"></label></div>'
        "<p>Follow Acme to stay up to date</p></div></div>"
    ),
}

FILLED = {
    "phone": "600000000",
    "notice": "48 hours",
    "languages": "Spanish",
    "consent": "checked",
    "consented": "true",
}
EMPTY = {
    "phone": "",
    "notice": "",
    "languages": "",
    "consent": "",
    "consented": "false",
}


def pages(values: dict[str, str], *pages: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(page, body=page["body"] % values) for page in pages]


# The posting, its Easy Apply button, and a dialog that behaves like the one
# measured. Every click that reaches it is logged on `window.log`.
DIALOG_SCRIPT = r"""
const PAGES = %s;
window.log = [];
const style = document.createElement('style');
// The measured inputs took no click of their own: only their wrapper did.
style.textContent = '.covered { pointer-events: none; }'
    + ' section.toasts:has([role="alert"]) { position: fixed; left: 0; right: 0; bottom: 0; height: 200px;'
    + ' background: white; z-index: 10; }';
document.head.appendChild(style);
const dialog = document.createElement('dialog');
dialog.setAttribute('data-testid', 'dialog');
dialog.setAttribute('aria-labelledby', 'dialog-header');
document.body.appendChild(dialog);
let index = 0;
const render = () => {
    const page = PAGES[index];
    const done = Math.floor(((index + 1) / PAGES.length) * 100);
    const last = index === PAGES.length - 1;
    const forward = last ? 'Submit application'
        : index === PAGES.length - 2 ? 'Review' : 'Next';
    dialog.innerHTML = '<button type="button" aria-label="Dismiss"></button>'
        + '<div data-testid="dialog-content"><h2 id="dialog-header">Apply to Acme</h2>'
        + '<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply">'
        + (page.bare ? '' : `<div><div id="pct">${done} percent complete</div>`
            + '<div aria-labelledby="pct"><svg role="progressbar" aria-valuemin="0"'
            + ` aria-valuemax="100" aria-valuenow="${done}"></svg>`
            + `</div><p>${index + 1}/${PAGES.length} pages</p></div>`)
        + '<div data-testid="lazy-column"><div>'
        + (page.title ? `<p>${page.title}</p>` : '') + page.body + '</div></div>'
        + '<div><footer>'
        + (index === 0 || last ? '' : '<button type="button">Back</button>')
        + `<button type="button">${forward}</button>`
        + '</footer></div></div></div>'
        + '<section class="toasts"><h2 data-testid="toasts-title">0 notifications</h2></section>';
    for (const wrapper of dialog.querySelectorAll('[role="checkbox"], [role="radio"]')) {
        wrapper.addEventListener('click', () => {
            const input = wrapper.querySelector('input');
            if (input.type === 'radio') {
                for (const other of dialog.querySelectorAll(`[name="${input.name}"]`)) {
                    other.checked = false;
                    other.closest('[role="radio"]').setAttribute('aria-checked', 'false');
                }
                input.checked = true;
            } else {
                input.checked = !input.checked;
            }
            wrapper.setAttribute('aria-checked', String(input.checked));
            window.log.push(input.checked ? `check ${input.id}` : `uncheck ${input.id}`);
            // Measured: the box comes back re-mounted, under React's next id.
            const label = wrapper.querySelector(`label[for="${input.id}"]`);
            input.id = input.id.replace('»', '+»');
            if (label) label.htmlFor = input.id;
        });
    }
    const upload = dialog.querySelector('#upload');
    if (upload) upload.addEventListener('click', () => {
        window.log.push('upload');
        const input = document.createElement('input');
        input.type = 'file';
        input.style.display = 'none';
        document.body.appendChild(input);
        input.addEventListener('change', () => {
            const name = input.files[0].name;
            for (const other of dialog.querySelectorAll('[name="resume"]')) {
                other.checked = false;
                other.closest('[role="radio"]').setAttribute('aria-checked', 'false');
            }
            dialog.querySelector('#easyApplyUploadedResumeRef').innerHTML =
                `<div role="radio" aria-label="${name}" aria-checked="true">`
                + `<input id="«r11»" type="radio" checked name="resume" class="covered">`
                + '<label for="«r11»"></label></div>';
            const toasts = dialog.querySelector('section.toasts');
            toasts.innerHTML = '<h2 data-testid="toasts-title">1 notification</h2>'
                + '<div role="alert"><p>Resume uploaded successfully</p>'
                + '<button type="button" aria-label="Dismiss"></button></div>';
            toasts.querySelector('button').addEventListener('click', () => {
                window.log.push('toast');
                toasts.innerHTML = '<h2 data-testid="toasts-title">0 notifications</h2>';
            });
        });
        input.click();
    });
    const buttons = dialog.querySelectorAll('footer button');
    buttons[buttons.length - 1].addEventListener('click', () => {
        if (last) {
            window.log.push('submit');
            dialog.close();
            dialog.remove();
            const sent = document.createElement('dialog');
            sent.textContent = 'Your application was sent to Acme';
            document.body.appendChild(sent);
            sent.showModal();
            return;
        }
        window.log.push('next');
        let valid = true;
        for (const id of page.required) {
            // By prefix: a clicked box has been re-mounted under a new id.
            const el = dialog.querySelector(`[id^="${id.slice(0, -1)}"]`);
            if (el.type === 'checkbox' ? el.checked : el.value) continue;
            valid = false;
            const error = document.createElement('p');
            error.textContent = 'This field is required';
            if (el.type === 'checkbox') {
                el.closest('fieldset').after(error);
            } else if (el.tagName === 'INPUT') {
                let helper = dialog.querySelector(`[id="${id}-info"]`);
                if (!helper) {
                    helper = document.createElement('div');
                    helper.id = `${id}-info`;
                    el.parentElement.after(helper);
                }
                helper.prepend(error);
            }
        }
        if (valid) setTimeout(() => { index += 1; render(); }, 100);
    });
};
document.querySelector('main button').addEventListener('click', () => {
    window.log.push('open');
    setTimeout(() => { render(); dialog.showModal(); }, 100);
});
dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    window.log.push('escape');
    if (window.stuck) return;
    const prompt = document.createElement('dialog');
    prompt.setAttribute('data-testid', 'dialog');
    prompt.innerHTML = '<button type="button" aria-label="Dismiss"></button>'
        + '<div data-testid="dialog-content"><div><h2>Save this application?</h2>'
        + '<p>If you choose to not save, your application will be discarded.</p>'
        + '<div><button type="button">Discard</button>'
        + '<button type="button">Save</button></div></div></div>';
    document.body.appendChild(prompt);
    const [discard, save] = prompt.querySelectorAll('[data-testid="dialog-content"] button');
    discard.addEventListener('click', () => {
        window.log.push('discard');
        prompt.remove();
        dialog.close();
        dialog.remove();
    });
    save.addEventListener('click', () => window.log.push('save'));
    prompt.showModal();
});
"""

EASY_APPLY_BUTTON = (
    '<button type="button" aria-label="Easy Apply to this job">'
    "<span>Easy Apply</span></button>"
)


def posting(steps: list[dict], *, stuck: bool = False) -> str:
    return (
        "<main>"
        f"<h1>AI Engineer</h1><p>Acme</p>{EASY_APPLY_BUTTON}"
        "<h2>About the job</h2><p>Build agents.</p>"
        '<a href="https://www.linkedin.com/jobs/view/456/">'
        "<p>Another role</p><p>Easy Apply</p></a>"
        "</main>"
        f"<script>window.stuck = {'true' if stuck else 'false'};"
        f"{DIALOG_SCRIPT % json.dumps(steps)}</script>"
    )


@pytest.fixture
async def dom_page():
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            page = await browser.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body="<p>ok</p>"
            ),
        )
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(job_pages, "_APPLY_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_OPEN_TIMEOUT", 2.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_STEP_TIMEOUT", 2.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_SET_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_DISCARD_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_UPLOAD_TIMEOUT", 3.0)
    monkeypatch.setattr(job_pages, "_EASY_APPLY_CONFIRMATION_TIMEOUT", 1.0)


async def reader_for(page, html: str) -> JobPageReader:
    await page.goto(JOB_URL)
    await page.set_content(html)
    session = PageSession(page)
    return JobPageReader(session, PageNavigator(session), PageContentReader(session))


async def read(page, html: str) -> EasyApplyFormRead:
    """Serve ``html`` as the posting and read its form the way the workflow does."""
    reader = await reader_for(page, html)
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return await reader.read_easy_apply_form(JOB_URL, "123", JOB_APPLY_EN_US)


async def submit(
    page, html: str, answers: dict[str, Any], *, confirm_submit: bool = False
) -> EasyApplySubmitRead:
    reader = await reader_for(page, html)
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return await reader.submit_easy_apply(
            JOB_URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=confirm_submit
        )


async def log(page) -> list[str]:
    """What reached the dialog, read in the page's own world where it was logged."""
    return await page.evaluate("() => window.log", isolated_context=False)


async def test_every_page_is_read_and_the_draft_discarded(dom_page):
    result = await read(
        dom_page, posting(pages(FILLED, CONTACT, RESUME, QUESTIONS, REVIEW))
    )

    assert result.type == "easy_apply"
    assert result.complete is True
    assert result.discarded is True
    assert [step.get("heading") for step in result.steps] == [
        "Contact info",
        "Resume",
        "Additional Questions",
        "Review your application",
    ]
    assert [step["progress"] for step in result.steps] == [25, 50, 75, 100]
    assert result.steps[0]["fields"] == [
        {
            "label": "First name",
            "type": "text",
            "required": True,
            "value": "Santiago ",
            "max_length": 100,
        },
        {
            "label": "Phone country code",
            "type": "select",
            "required": True,
            "options": ["Spain (+34)", "France (+33)"],
            "value": "France (+33)",
        },
        {
            "label": "Mobile phone number",
            "type": "tel",
            "required": True,
            "value": "600000000",
        },
        # Titled by a paragraph, not by the page's own title before it.
        {"label": "Location (city)", "type": "typeahead", "required": False},
    ]
    assert result.steps[1]["fields"] == [
        {
            "label": "Resume",
            "type": "radio",
            "required": True,
            "options": ["old.pdf"],
            "selected": ["old.pdf"],
        },
        {"label": "Upload resume", "type": "file", "required": False},
        {"label": "Headline", "type": "text", "required": False, "max_length": 127},
    ]
    assert result.steps[2]["fields"] == [
        {
            "label": "Notice Period",
            "type": "text",
            "required": True,
            "value": "48 hours",
            "max_length": 127,
        },
        {
            "label": "Spoken Languages",
            "type": "textarea",
            "required": True,
            "value": "Spanish",
        },
        # Required by its title's asterisk alone.
        {
            "label": "Personal data consent",
            "type": "checkbox",
            "required": True,
            "options": ["I consent to the processing of my data."],
            "selected": ["I consent to the processing of my data."],
        },
    ]
    # The follow checkbox is not a question.
    assert result.steps[3]["fields"] == []
    assert await log(dom_page) == [
        "open",
        "next",
        "next",
        "next",
        "escape",
        "discard",
    ]


async def test_a_page_needing_input_ends_the_read_with_its_error(dom_page):
    result = await read(dom_page, posting(pages(EMPTY, CONTACT, RESUME, REVIEW)))

    assert result.complete is False
    assert result.discarded is True
    assert len(result.steps) == 1
    assert result.steps[0]["fields"][2] == {
        "label": "Mobile phone number",
        "type": "tel",
        "required": True,
        "error": "This field is required",
    }
    assert await log(dom_page) == ["open", "next", "escape", "discard"]


async def test_the_last_page_is_never_moved_on_from(dom_page):
    result = await read(dom_page, posting(pages(FILLED, REVIEW)))

    assert result.complete is True
    assert result.steps[0]["progress"] == 100
    assert await log(dom_page) == ["open", "escape", "discard"]


async def test_a_dialog_that_will_not_close_is_reported(dom_page):
    result = await read(dom_page, posting(pages(FILLED, REVIEW), stuck=True))

    assert result.discarded is False
    assert "discard" not in await log(dom_page)
    assert "save" not in await log(dom_page)


async def test_a_rehearsal_answers_every_page_and_sends_nothing(dom_page, tmp_path):
    resume = tmp_path / "Acme resume.pdf"
    resume.write_bytes(b"%PDF-1.7")

    result = await submit(
        dom_page,
        posting(pages(EMPTY, CONTACT, RESUME, QUESTIONS, REVIEW)),
        {
            "First name": "Santiago",
            "Mobile phone number": "600000000",
            "Upload resume": str(resume),
            "Notice Period": "48 hours",
            "Spoken Languages": "Spanish, French",
            "Personal data consent": True,
        },
    )

    assert result.complete is True
    assert result.submitted is False
    assert result.discarded is True
    assert result.follow is False
    assert result.steps[0]["fields"][0]["value"] == "Santiago"
    assert result.steps[1]["fields"][0]["selected"] == ["Acme resume.pdf"]
    assert result.steps[2]["fields"][2]["selected"] == [
        "I consent to the processing of my data."
    ]
    # The toast over the footer is closed before Next is clicked.
    assert await log(dom_page) == [
        "open",
        "next",
        "upload",
        "toast",
        "next",
        "check «r9»",
        "next",
        "uncheck «r10»",
        "escape",
        "discard",
    ]


async def test_the_flag_sends_it_from_the_last_page_only(dom_page):
    result = await submit(
        dom_page,
        posting(pages(FILLED, CONTACT, REVIEW)),
        {},
        confirm_submit=True,
    )

    assert result.submitted is True
    assert result.confirmation == "Your application was sent to Acme"
    assert await log(dom_page) == ["open", "next", "uncheck «r10»", "submit"]


async def test_a_last_page_without_progress_is_not_sent(dom_page):
    """Without its progress the forward button could be Next as well as Submit."""
    with pytest.raises(RuntimeError, match="Nothing was sent"):
        await submit(
            dom_page,
            posting(pages(FILLED, dict(REVIEW, bare=True))),
            {},
            confirm_submit=True,
        )

    assert "submit" not in await log(dom_page)
    assert (await log(dom_page))[-2:] == ["escape", "discard"]
