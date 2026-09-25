# Easy Apply markup, as measured

What LinkedIn's Easy Apply dialogs publish, read off a live page rather than
assumed. Every line here carries the date and the posting it came from, and a
later reading that disagrees replaces it.

There are two dialogs, and the posting's apply control says which one it
opens. An anchor into the posting's own `/apply/` route opens the dialog in the
shadow root described first. A `<button>` with no href, measured from
2026-09-25, opens the redesigned dialog described under
[The redesigned dialog](#the-redesigned-dialog).

Measured 2026-09-23 against job posting `4412345678`, a posting whose
first step is "Contact info", signed in as an ordinary member. The dialog was
opened and its draft discarded; no step was advanced.

## The dialog lives in an open shadow root

The whole dialog renders inside the open shadow root of `div#interop-outlet`,
a host that is present on the posting page before the dialog exists. With the
dialog open, the document itself sees none of it:

| query on `document` | result |
| --- | --- |
| `[role="dialog"]` | 0 |
| `[role="combobox"]` | 0 |
| `[data-easy-apply-next-button]` | 0 |
| `[role="option"]` (list open) | 0 |

The root is open, so a CSS locator reaches through it and a hand-written
`document.querySelector` does not. That is the whole reason the dialog is
addressed with Playwright locators and the programs that do run in the page
walk `element.shadowRoot` themselves.

## The typeahead control

```html
<input id="single-typeahead-entity-form-component-formElement-urn-li-jobs-applyformcommon-easyApplyFormElement-4412345678-31234567890-location-GEO-LOCATION"
       class="basic-input " required="" dir="auto" role="combobox"
       aria-autocomplete="list" aria-activedescendant="" aria-expanded="false"
       type="text">
```

The id names the form element and the question (`...-location-GEO-LOCATION`),
and the wrapper `div` around the input repeats it with a `-ta` suffix. It is
LinkedIn's own handle, and nothing reads it apart from the step read, which
hands it back so an answer can reach the control it came from.

- `role="combobox"` is what separates it from a plain text input, and it is
  present before anything is typed. It is what the step read classifies on.
- `aria-activedescendant` is present and stays empty, with the list open and
  no arrow key pressed. Nothing in the DOM says which option a keystroke would
  take.
- The label beside it says its text twice, once `aria-hidden="true"` and once
  `class="visually-hidden"`. Read raw it doubles.

**No control carries the entity.** The question block holds no
`input[type="hidden"]`, and no other element carries the geo URN; the binding
between the visible string and the entity lives in component state only. There
is therefore no DOM reading of whether a pick took, beyond the field's value
and whether the list closed.

## The suggestion list

Typing opens a `div[role="listbox"]` with an id of the form
`triggered-expanded-ember<N>`, **inside the shadow root, as a sibling of the
input** within the field's wrapper `div` — not portalled to the document body.
While it is open the input carries both `aria-owns` and `aria-controls`, each
holding that same id; both are absent while it is closed.

Options are `div[role="option"][aria-selected="false"]` with ids of the form
`basic-result-<token>`. They are not list items. Each wraps its text in nested
spans across several lines, so the node's `textContent` is the string padded
with newlines and indentation; collapsing whitespace is required to match it.
The option itself carries no `aria-hidden` copy of its text, where the label
does.

Suggestions arrived within 300ms of the last keystroke.

## The vocabulary

`Paris` returned 20 suggestions, in this order at the top:

1. `Paris, Île-de-France, France`
2. `Paris, Texas, United States`
3. `Paris, Tennessee, United States`
4. `Orleans Parish County, Louisiana, United States`

The rest were further `Paris,` municipalities and Louisiana parishes. The
first is character for character the location the account's profile
carried, so a value read off the profile names a real suggestion and
would have been accepted. The ranking answers to the query alone and not to
where the member is: an account outside the United States asking for `Paris`
is offered American counties above `Paris, Ontario, Canada`, and only an
answer written in full reaches the one it means.

## What a prefill is worth

On `4412345678` the field was empty and required, although the same account
had sent an Easy Apply application four days earlier on a posting whose contact
step carried the same question and could not have been sent without answering
it. A past answer is therefore not reliably prefilled into a later form, and a
form that asks is asking for real. A second posting read the same day,
`4487654321`, had no location question on its contact step at all, so which
questions a step carries is the posting's own.

The account was not opted out of it: LinkedIn's own "Resumes and application
data" setting read **On** the same day, under the words "Allow LinkedIn to save
your resumes and answers to application questions". Saving being on is
therefore not the same as this answer coming back.

## Not measured

Whether advancing a step saves the answer to the account, or whether only
submitting does. Answering it costs a step advanced on a real posting.

## The redesigned dialog

Measured 2026-09-25 against a six-page job posting, signed in as an ordinary
member in en-US. Every page was reached: page 1 by typing the phone number,
page 2 by uploading a resume, page 6 by answering the questions on page 5. Submit was not clicked; the draft was discarded and the
posting read as open afterwards.

### Opening it

The posting's control is
`<button type="button" aria-label="Easy Apply to this job">` whose text is
"Easy Apply". It carries no href, no data attribute and nothing naming the
posting.

### The dialog

A native `<dialog open data-testid="dialog" aria-labelledby="dialog-header">`,
a child of `div#root` in the document, matching `:modal`. No shadow root is
involved, and no element carries `role="dialog"`. The posting page holds no
`<dialog>` before the click.

`data-testid="dialog"` is LinkedIn's generic dialog and not specific to Easy
Apply. What is specific is a descendant
`div[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply"]`,
present on every page.

There is no `<form>` and no `<fieldset>` around the page. The header `h2` is
"Apply to <company>". The page title ("Contact info", "Additional Questions")
is a `<p>`, the first child of `div[data-testid="lazy-column"]`; only the last
page uses a heading, an `h3` "Review your application" with `h4` section
titles.

### Progress

An `svg[role="progressbar"]` with `aria-valuemin="0"`, `aria-valuemax="100"`
and `aria-valuenow` at 16, 33, 50, 66, 83 and 100 across the six pages: the
page's share of the total, truncated. The same box shows the text "n/6 pages"
in a `<p>`, and a visually hidden "16 percent complete" names the bar.

### The footer

A `<footer>` holding only `<button type="button">` elements. None carries a
data attribute or an `aria-label`; each has a `componentkey` that is a UUID,
stable for one button across pages (Next and Back kept theirs) and different
between buttons.

| page | buttons, in DOM order |
| --- | --- |
| 1 | Next |
| 2–4 | Back, Next |
| 5 | Back, Review |
| 6 | Submit application |

The button that moves forward is always the last one. It is Submit on the page
whose progress reads 100, and only there. "Review" leads to the last page
rather than submitting anything.

### Fields

A question sits in a wrapper whose `componentkey` starts with
`easyApplyFieldFocus_`. Two forms were seen:
`easyApplyFieldFocus_ea.q::<question id>::<TYPE>::<name>.validation`
(`FIRST_NAME`, `LAST_NAME`, `PHONE_MOBILE`, `UNKNOWN`) on page 1, and
`easyApplyFieldFocus_ea_validation_p<page>_0_<question id>` from page 2 on,
where a `<textarea>` carries the key itself. The two selects on page 1 (phone
country code and email) sit in no such wrapper.

Control ids are React's generated `«r8»` form, unique within the dialog and
renumbered each time it opens.

The label comes in four shapes:

- `<label for>` holding a `<div>` with the text: first name, last name, phone,
  both selects, "Photo".
- `aria-label` on the control and no `<label>`: "Headline", "Summary", the page
  5 questions.
- A `<p>` before the control, inside the wrapper or its parent, and nothing on
  the control: "Location (city)", the page 5 questions (again, besides their
  `aria-label`), "Personal data consent".
- For a radio or checkbox, a `div[role="radio"]` or `div[role="checkbox"]`
  around the input, carrying `aria-label` and `aria-checked`. The input's own
  `<label for>` is empty.

Whether a question is required shows as `required` on text inputs, selects and
textareas. A required checkbox carries no attribute at all: the only sign is
the `*` ending its title `<p>` ("Personal data consent*"). Titles of required
text questions end in `*` too.

Neither `maxlength` nor `aria-invalid` is set. The limit is only text in a
`div[data-testid="text-input-helper-text"]` ("0/127", "0 of 127 characters"),
which the input names in `aria-describedby` as `<id>-info`. Textareas name an
`<id>-info` that does not exist.

### Errors

Clicking Review with page 5 empty kept the page (progress stayed at 83) and
added a `<p>` "This field is required" in two places only:

- inside the text input's `<id>-info` helper, before the character count;
- after the `<fieldset>` of the consent checkbox, inside its wrapper, pointed
  at by nothing. The `fieldset`'s `aria-describedby` names an
  `error-message-«rv»` that does not exist.

The two empty required textareas showed no error anywhere. A refused page is
therefore told by the page not moving, not by errors being present.

### Controls that need care

- Checkboxes: the input has `tabindex="-1"` and `Locator.check()` timed out on
  it. Clicking its `div[role="checkbox"]` checked it, and both the input's
  `checked` and the wrapper's `aria-checked` followed.
- Location (city): `input[data-testid="typeahead-input"]` with
  `aria-autocomplete="list"`, `autocomplete="off"`, a placeholder, no `role`,
  no `required`. Left empty, the review page still showed
  `urn:li:geo:106383538` for it, so LinkedIn filled it from the profile without
  showing it in the field. Its suggestions were not opened.
- Page 1's first name was prefilled with a trailing space ("Ada ").

### The resume page

A `fieldset[role="radiogroup"]` with no legend, the title "Resume*" in a `<p>`
outside it. Each resume is a card (`div[role="button"]` holding the file name
and date) beside a `div[role="radio"]` whose `aria-label` is the file name,
wrapping the unlabelled `input[type="radio"]`. The first card is preselected;
it was the resume most recently used.

The upload is a `<button>` "Upload resume" beside the fieldset, the only button
on the page outside the footer without an `aria-label`. There is no
`input[type="file"]` in the page until it is clicked: the click appends a
hidden `<input type="file" multiple>` to `<body>`, outside the dialog, and
opens the file chooser from it. Setting a file on that chooser uploaded it
within a second. The new resume became the first card, inside
`div#easyApplyUploadedResumeRef`, already checked.

The upload also raised a toast inside the dialog: a `<section>` holding
`h2[data-testid="toasts-title"]` ("1 notification") and a `div[role="alert"]`
"Resume uploaded successfully" with its own `button[aria-label="Dismiss"]`. It
sat over the footer and intercepted the click on Next. It was still there ten
seconds later; its dismiss button removed it.

### The review page

Every answer read back as a `<p>` pair (title, value), with an Edit button per
section. The only control is the follow checkbox:
`div[role="checkbox"][aria-label="Follow <company> to stay up to date with
their page"][aria-checked="true"]` around an `input[type="checkbox"]`, checked
by default. It sits in the page body, not in the footer, and in no
`easyApplyFieldFocus_` wrapper.

### Closing it

Escape leaves the form open and adds a second modal
`<dialog open data-testid="dialog">`, without `aria-labelledby` and without the
Easy Apply screen inside it: an `h2` "Save this application?", a paragraph, and
two `<button type="button">` in `div[data-testid="dialog-content"]`, Discard
first and Save second, each with a UUID `componentkey` and nothing else.
Clicking the first removed both dialogs from the document.

### Ids that move

Clicking a radio's or checkbox's wrapper re-mounts its input under React's
next id: the consent box went `«r10»`, `«r11»`, `«r12»`, `«r13»` over three
clicks. An id read before a click names nothing after it. Typing into a text
field or textarea kept every id on the page.

### After Submit

Sent 2026-09-25 on the same posting. The form closed, and the posting then
showed "Application status" and "Application submitted" above its
description. No dialog with text was open when the confirmation was read a
moment later, so the posting's line is what answered.

### Not measured

- A typeahead's suggestion list, and whether a `role="option"` list appears.
- A select, radio or checkbox question beyond the ones above, and a one-page
  form.
- Whether an answer typed and then discarded comes back as a prefill.
- Any locale other than en-US.
