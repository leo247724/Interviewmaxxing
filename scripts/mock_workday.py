"""Workday-style application wizard for the mock ATS (scenario ``workday-wizard``).

Standard library only; loaded by ``scripts/mock_ats.py``, which owns the server, the
routes and the shared ``Store``. Everything here is fictional (Brambleway Analytics).

The shapes follow the live Workday candidate site as observed read-only on
2026-09-24 (four public postings, up to the account step):

* The posting's "Apply" is ``a[role=button][data-automation-id=adventureButton]`` to
  ``<posting>/apply``. Clicked, it opens a ``role=dialog`` "Start Your Application" popup
  in the page (the URL stays, the root turns aria-hidden) with three ``a[role=button]``
  routes: ``autofillWithResume``, ``applyManually`` and ``useMyLastApplication``. Opened
  as a URL, ``<posting>/apply`` shows the same routes on a page of its own
  (``applyAdventurePage``).
* ``<posting>/apply/applyManually`` (signed out) is the account step: a progress list
  (``ol[data-automation-id=progressBar]``, each item labelled "current step 1 of 7",
  "step 2 of 7" ...), the job title (``h2[data-automation-id=jobTitleHeading]``), a
  "Create Account" form (Email Address, Password, Verify New Password, a consent
  checkbox and a ``click_filter`` overlay over a hidden submit button), "Already have an
  account? Sign In" and "Forgot your password?" outside the form, and a zero-height
  honeypot ``input[name=website]`` labelled "Enter website. This input is for robots
  only, do not enter if you're human." The document keeps the posting's JSON-LD.

Behind the account step nothing could be observed without an account, so the wizard
reproduces Workday's documented page set and widget conventions: My Information, My
Experience, Application Questions, Voluntary Disclosures, Self Identify and Review in
one document (the URL never changes), "Save and Continue" / "Back" / "Submit" footer
buttons, ``button[aria-haspopup=listbox]`` dropdowns whose listbox is a body portal
named by ``aria-controls`` while open, a multi-select "prompt" (search box, results only
after Enter, checkbox options, chosen items as pills), Month / Day / Year spinbutton
dates, a country phone code dropdown beside the number, a drop-zone uploader that sends
the file on attach and empties its input, an error banner plus inline errors, and a
"<page> page is loaded" announcement. Saving a page stores a draft; only "Submit"
records (and counts) an application.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any

SLUG = "workday-wizard"
CODE = "JR-BWA-201"
TITLE = "Growth Marketing Manager"
LOCATION = "Denver, CO"
COMPANY = "Brambleway Analytics"
SESSION_COOKIE = "bwa_wd_session"
ROUTES = ("autofillWithResume", "applyManually", "useMyLastApplication")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _options(*labels: str, prefix: str) -> list[list[str]]:
    """Options with opaque Workday-style machine values (``wd_<prefix>_<n>``)."""
    return [[f"wd_{prefix}_{i}", label] for i, label in enumerate(labels, start=1)]


COUNTRIES = _options("United States of America", "Canada", "United Kingdom", "Germany", prefix="country")
STATES = _options("California", "Colorado", "New York", "Texas", "Washington", prefix="state")
PHONE_TYPES = _options("Mobile", "Home", "Work", prefix="phonetype")
PHONE_CODES = _options("United States of America (+1)", "Canada (+1)", "United Kingdom (+44)",
                       "Germany (+49)", prefix="phonecode")
YES_NO = _options("Yes", "No", prefix="yn")
YEARS = _options("0-2 years", "3-5 years", "6-9 years", "10+ years", prefix="years")
GENDERS = _options("Female", "Male", "I do not wish to self-identify", prefix="gender")
ETHNICITIES = _options("Hispanic or Latino", "White (Not Hispanic or Latino)",
                       "Black or African American (Not Hispanic or Latino)",
                       "Asian (Not Hispanic or Latino)", "Two or More Races (Not Hispanic or Latino)",
                       "I do not wish to self-identify", prefix="ethnicity")
VETERAN = _options("I am not a protected veteran",
                   "I identify as one or more of the classifications of protected veteran",
                   "I do not wish to self-identify", prefix="veteran")
DISABILITY = _options("Yes, I have a disability, or have had one in the past",
                      "No, I do not have a disability and have not had one in the past",
                      "I do not want to answer", prefix="disability")
# The prompt's browse tree: categories drill down to leaves; search lists leaves only.
SOURCES = [
    {"label": "Job Board", "children": ["LinkedIn", "Indeed", "Glassdoor"]},
    {"label": "Company Website", "children": ["Brambleway Careers Website"]},
    {"label": "Referral", "children": ["Employee Referral", "Friend or Family"]},
    {"label": "Other", "children": ["Other"]},
]
SOURCE_LEAVES = [leaf for group in SOURCES for leaf in group["children"]]

PAGES: list[dict[str, Any]] = [
    {"id": "myInformation", "title": "My Information", "fields": [
        {"name": "source", "id": "source--source", "kind": "prompt", "required": True,
         "label": "How Did You Hear About Us?", "tree": SOURCES},
        {"name": "candidateIsPreviousWorker", "id": "candidateIsPreviousWorker", "kind": "radio",
         "required": True, "label": f"Have you previously worked for {COMPANY}?",
         "options": [["true", "Yes"], ["false", "No"]]},
        {"name": "country", "id": "country--country", "kind": "dropdown", "required": True,
         "label": "Country", "options": COUNTRIES, "default": COUNTRIES[0][0]},
        {"name": "firstName", "id": "name--legalName--firstName", "kind": "text", "required": True,
         "label": "First Name", "section": "Legal Name"},
        {"name": "lastName", "id": "name--legalName--lastName", "kind": "text", "required": True,
         "label": "Last Name", "section": "Legal Name"},
        {"name": "addressLine1", "id": "address--addressLine1", "kind": "text", "required": True,
         "label": "Address Line 1", "section": "Address"},
        {"name": "city", "id": "address--city", "kind": "text", "required": True, "label": "City",
         "section": "Address"},
        {"name": "state", "id": "address--countryRegion", "kind": "dropdown", "required": True,
         "label": "State", "options": STATES, "section": "Address"},
        {"name": "postalCode", "id": "address--postalCode", "kind": "text", "required": True,
         "label": "Postal Code", "section": "Address"},
        {"name": "email", "id": "emailAddress--emailAddress", "kind": "text", "required": True,
         "label": "Email Address", "section": "Email Address", "prefill": "account_email"},
        {"name": "phoneType", "id": "phoneNumber--phoneType", "kind": "dropdown", "required": True,
         "label": "Phone Device Type", "options": PHONE_TYPES, "section": "Phone"},
        {"name": "countryPhoneCode", "id": "phoneNumber--countryPhoneCode", "kind": "prompt", "single": True,
         "required": True, "label": "Country Phone Code", "options": PHONE_CODES,
         "default": [PHONE_CODES[0][1]], "section": "Phone"},
        {"name": "phoneNumber", "id": "phoneNumber--phoneNumber", "kind": "text", "required": True,
         "label": "Phone Number", "section": "Phone"},
        {"name": "extension", "id": "phoneNumber--extension", "kind": "text", "required": False,
         "label": "Phone Extension", "section": "Phone"},
    ]},
    {"id": "myExperience", "title": "My Experience", "fields": [
        {"kind": "add_section", "name": "workExperience", "label": "Work Experience"},
        {"name": "resume", "id": "resumeAttachments--attachments", "kind": "file", "required": True,
         "label": "Resume/CV", "accept": ".pdf,.doc,.docx"},
        {"name": "linkedin", "id": "socialNetworkAccounts--linkedInAccount", "kind": "text",
         "required": False, "label": "LinkedIn", "section": "Social Network URLs"},
    ]},
    {"id": "primaryQuestionnaire", "title": "Application Questions", "fields": [
        {"name": "workAuthorization", "id": "primaryQuestionnaire--q1", "kind": "dropdown",
         "required": True, "label": "Are you legally authorized to work in the United States?",
         "options": YES_NO},
        {"name": "sponsorship", "id": "primaryQuestionnaire--q2", "kind": "dropdown", "required": True,
         "label": "Will you now or in the future require sponsorship for employment visa status?",
         "options": YES_NO},
        {"name": "growthYears", "id": "primaryQuestionnaire--q3", "kind": "dropdown", "required": True,
         "label": "How many years of B2B growth marketing experience do you have?", "options": YEARS},
    ]},
    {"id": "voluntaryDisclosures", "title": "Voluntary Disclosures", "fields": [
        {"name": "gender", "id": "personalInfoUS--gender", "kind": "dropdown", "required": True,
         "label": "Gender", "options": GENDERS},
        {"name": "ethnicity", "id": "personalInfoUS--ethnicity", "kind": "dropdown", "required": True,
         "label": "Ethnicity", "options": ETHNICITIES},
        {"name": "veteranStatus", "id": "personalInfoUS--veteranStatus", "kind": "dropdown",
         "required": True, "label": "Veteran Status", "options": VETERAN},
        {"name": "acceptTerms", "id": "termsAndConditions--acceptTermsAndAgreements", "kind": "checkbox",
         "required": True, "label": "Yes, I have read and consent to the terms and conditions",
         "terms": f"{COMPANY} collects this information for its equal employment opportunity "
                  "reporting. Providing it is voluntary."},
    ]},
    {"id": "selfIdentify", "title": "Self Identify", "fields": [
        {"name": "selfIdName", "id": "selfIdentifiedDisabilityData--name", "kind": "text",
         "required": True, "label": "Name"},
        {"name": "selfIdDate", "id": "selfIdentifiedDisabilityData--dateSignedOn", "kind": "date",
         "required": True, "label": "Date"},
        {"name": "disabilityStatus", "id": "selfIdentifiedDisabilityData--disabilityStatus",
         "kind": "checkgroup", "required": True, "label": "Disability Status",
         "hint": "Please check one of the boxes below:", "options": DISABILITY},
    ]},
]
PAGE_IDS = [p["id"] for p in PAGES]
STEP_TITLES = ["Create Account/Sign In", *(p["title"] for p in PAGES), "Review"]
"""The progress list: the account step, the five question pages and Review (7 steps)."""


def prompt_leaves(f: dict[str, Any]) -> list[str]:
    """What a prompt offers: the leaves of its browse tree, or its option labels."""
    if f.get("tree"):
        return [leaf for group in f["tree"] for leaf in group["children"]]
    return [label for _, label in f.get("options") or []]


def field_catalog() -> list[dict[str, Any]]:
    """Every wizard question for ``/__test__/jobs`` (page, name, label, kind, required,
    option value/label pairs)."""
    out = []
    for page in PAGES:
        for f in page["fields"]:
            if f["kind"] == "add_section":
                continue
            options = [[leaf, leaf] for leaf in prompt_leaves(f)] if f["kind"] == "prompt" else f.get("options") or []
            out.append({"page": page["id"], "name": f["name"], "id": f["id"], "label": f["label"],
                        "kind": f["kind"], "required": f["required"],
                        "options": [{"value": v, "label": lbl} for v, lbl in options]})
    return out


# --- validation ---------------------------------------------------------------------------

_PHONE_DIGITS = re.compile(r"^[\d\s().\-]+$")


def validate_page(page_id: str, values: dict[str, Any], resume: dict[str, Any] | None) -> dict[str, str]:
    """Server-side checks of one saved page: required answers, listed options, a US
    phone number without its country code, a real calendar date, one disability box."""
    page = next((p for p in PAGES if p["id"] == page_id), None)
    if page is None:
        return {"_page": "Unknown page."}
    errors: dict[str, str] = {}
    for f in page["fields"]:
        kind, name = f["kind"], f.get("name", "")
        if kind == "add_section":
            continue
        value = values.get(name)
        label = f["label"]
        empty = value in (None, "", [], False)
        if kind == "file":
            empty = resume is None
        if empty:
            if f["required"]:
                errors[name] = f"The field {label} is required and must have a value."
            continue
        if kind in ("dropdown", "radio"):
            if value not in [v for v, _ in f["options"]]:
                errors[name] = f"{label}: select one of the listed options."
        elif kind == "checkgroup":
            chosen = value if isinstance(value, list) else [value]
            if len(chosen) != 1 or chosen[0] not in [v for v, _ in f["options"]]:
                errors[name] = f"{label}: check exactly one box."
        elif kind == "prompt":
            chosen = value if isinstance(value, list) else [value]
            if not chosen or any(c not in prompt_leaves(f) for c in chosen) or (f.get("single") and len(chosen) != 1):
                errors[name] = f"{label}: select an item from the list."
        elif kind == "checkbox":
            if value is not True:
                errors[name] = f"The field {label} is required and must have a value."
        elif kind == "date":
            try:
                month, day, year = (int(p) for p in str(value).split("/"))
                date(year, month, day)
            except (ValueError, TypeError):
                errors[name] = f"{label}: enter a valid date as MM/DD/YYYY."
        elif name == "phoneNumber":
            text = str(value).strip()
            if text.startswith("+"):
                errors[name] = ("Phone Number: enter the number without the country phone code; "
                                "choose it in Country Phone Code.")
            elif not _PHONE_DIGITS.match(text) or len(re.sub(r"\D", "", text)) != 10:
                errors[name] = "Phone Number: enter a 10-digit phone number."
        elif kind == "text" and name == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", str(value)):
            errors[name] = "Email Address: enter a valid email address."
    return errors


def display_value(page_id: str, name: str, value: Any, resume: dict[str, Any] | None) -> str:
    page = next(p for p in PAGES if p["id"] == page_id)
    f = next(x for x in page["fields"] if x.get("name") == name)
    if f["kind"] == "file":
        return resume["filename"] if resume else ""
    if f["kind"] in ("dropdown", "radio", "checkgroup"):
        labels = dict((v, lbl) for v, lbl in f["options"])
        items = value if isinstance(value, list) else [value]
        return ", ".join(labels.get(v, str(v)) for v in items if v not in (None, ""))
    if f["kind"] == "prompt":
        return ", ".join(value if isinstance(value, list) else [value])
    if f["kind"] == "checkbox":
        return "Yes" if value is True else ""
    return "" if value is None else str(value)


# --- pages -------------------------------------------------------------------------------

STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;color:#333d47;background:#f0f1f2;line-height:1.45}
header.wd-header{display:flex;align-items:center;gap:1rem;background:#fff;border-bottom:1px solid #ced3d9;padding:.6rem 1.5rem}
header.wd-header .wd-logo{font-weight:700;color:#0875e1;text-decoration:none;margin-right:auto}
header.wd-header button{background:none;border:0;color:#333d47;font:inherit;cursor:pointer}
main{max-width:52rem;margin:1rem auto;background:#fff;padding:1.5rem 2rem;border-radius:8px}
.wd-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.wd-button{background:#0875e1;color:#fff;border:0;border-radius:18px;padding:.5rem 1.4rem;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
.wd-secondary{background:#fff;color:#333d47;border:1px solid #7b858f}
ol.wd-progress{display:flex;gap:.4rem;list-style:none;padding:0;margin:1rem 0 1.5rem;font-size:.8rem}
ol.wd-progress li{flex:1;border-top:4px solid #ced3d9;padding-top:.3rem}
ol.wd-progress li[data-automation-id=progressBarActiveStep]{border-color:#0875e1;font-weight:700}
ol.wd-progress li[data-automation-id=progressBarCompletedStep]{border-color:#43a047}
.wd-field{margin:1rem 0}
.wd-field>label,.wd-field legend,.wd-field .wd-label{display:block;font-weight:600;margin-bottom:.25rem}
.wd-field fieldset{border:0;padding:0;margin:0}
.wd-field input[type=text],.wd-field input[type=password]{width:22rem;max-width:100%;box-sizing:border-box;padding:.4rem;border:1px solid #7b858f;border-radius:4px;font:inherit}
.wd-field [aria-invalid=true]{border-color:#de2e21;outline:2px solid #de2e21}
.wd-error{color:#de2e21;font-size:.85rem;margin-top:.2rem}
button.wd-select{min-width:22rem;text-align:left;background:#fff;border:1px solid #7b858f;border-radius:4px;padding:.4rem;font:inherit;color:#333d47;cursor:pointer}
.wd-popup-list{position:absolute;z-index:40;background:#fff;border:1px solid #7b858f;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.25);margin:0;padding:.2rem 0;list-style:none;min-width:22rem;max-height:18rem;overflow:auto}
.wd-popup-list [role=option]{padding:.35rem .7rem;cursor:pointer;display:flex;gap:.5rem;align-items:center}
.wd-popup-list [role=option][aria-selected=true]{background:#e6f1fc}
.wd-popup-list .wd-note{padding:.35rem .7rem;color:#5e6a75;font-size:.85rem}
.wd-check{display:inline-block;width:14px;height:14px;border:1px solid #7b858f;border-radius:2px}
[aria-selected=true]>.wd-check{background:#0875e1;border-color:#0875e1}
.wd-prompt{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem;min-width:22rem;width:22rem;border:1px solid #7b858f;border-radius:4px;padding:.25rem}
.wd-prompt input{border:0;outline:0;flex:1;min-width:6rem;font:inherit;padding:.15rem}
ul.wd-pills{display:contents;list-style:none;margin:0;padding:0}
ul.wd-pills li{display:inline-flex;align-items:center;gap:.2rem;background:#e6f1fc;border-radius:10px;padding:0 .5rem;font-size:.85rem}
ul.wd-pills button{background:none;border:0;cursor:pointer;padding:0 .1rem}
.wd-charm{display:inline-block;width:12px;height:12px;cursor:pointer}
.wd-charm::before{content:"\\00d7"}
.wd-date{display:inline-flex;align-items:center;gap:.15rem;border:1px solid #7b858f;border-radius:4px;padding:.2rem .4rem}
.wd-date input{width:2.4rem;border:0;outline:0;font:inherit;text-align:center}
.wd-date input[data-automation-id=dateSectionYear-input]{width:3.4rem}
.wd-date button{background:none;border:0;cursor:pointer}
.wd-drop{border:1px dashed #7b858f;border-radius:8px;padding:1rem;text-align:center}
.wd-drop label.wd-button{cursor:pointer}
.wd-file-item{display:flex;gap:1rem;align-items:center;margin-top:.5rem}
.wd-footer{display:flex;justify-content:space-between;margin-top:2rem;border-top:1px solid #ced3d9;padding-top:1rem}
.wd-banner{border:2px solid #de2e21;border-radius:6px;padding:.5rem 1rem;margin-bottom:1rem;color:#de2e21}
.wd-banner button{background:none;border:0;color:#de2e21;font-weight:700;cursor:pointer;padding:0}
.wd-glass{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;z-index:60}
.wd-dialog{background:#fff;border-radius:8px;padding:40px 24px 24px;position:relative;min-width:24rem;display:flex;flex-direction:column;gap:.8rem}
.wd-dialog .wd-close{position:absolute;right:8px;top:8px;background:none;border:0;font-size:1.3rem;cursor:pointer}
.wd-honeypot{position:absolute;height:0;overflow:hidden}
.wd-honeypot input{height:0;padding:0;border:0}
dl.wd-review dt{font-weight:600;margin-top:.5rem}
dl.wd-review dd{margin:0}
"""


def _document(title: str, body: str, *, origin: str, glass: str = "") -> str:
    ld = {
        "@context": "https://schema.org", "@type": "JobPosting", "title": TITLE,
        "identifier": {"@type": "PropertyValue", "name": TITLE, "value": CODE},
        "hiringOrganization": {"@type": "Organization", "name": COMPANY},
        "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress",
                                                        "addressLocality": LOCATION,
                                                        "addressCountry": "United States of America"}},
        "employmentType": "FULL_TIME", "datePosted": "2026-09-17",
        "description": f"{TITLE} on the Marketing team at {COMPANY}.",
    }
    return f"""<!doctype html>
<html lang="en-US">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} | {COMPANY} Careers | Workday-style mock</title>
<link rel="canonical" href="{esc(origin)}/jobs/{SLUG}">
<script type="application/ld+json">{json.dumps(ld).replace("</", "<\\/")}</script>
<style>{STYLE}</style>
</head>
<body>
<div id="root">
<header class="wd-header"><a class="wd-logo" href="/jobs/{SLUG}">{COMPANY} Careers</a>
<button type="button" data-automation-id="languagePicker">English</button>
<button type="button" data-automation-id="utilityButtonSignIn">Sign In</button>
<nav><button type="button" data-automation-id="navigationItem-Search for Jobs">Search for Jobs</button></nav></header>
<main>
{body}
</main>
<div data-automation-id="footerContainer"><p>© 2026 Workday-style fictional site for local software testing.</p></div>
</div>
{glass}
</body>
</html>
"""


def _posting_body() -> str:
    return (
        f'<span role="alert" class="wd-sr">{esc(TITLE)} page is loaded</span>'
        f'<h2 data-automation-id="jobPostingHeader">{esc(TITLE)}</h2>'
        f'<div><a href="/jobs/{SLUG}/apply" role="button" data-automation-id="adventureButton" '
        'class="wd-button">Apply</a></div>'
        '<dl data-automation-id="job-posting-details">'
        "<dt>remote type</dt><dd>Hybrid</dd>"
        f"<dt>locations</dt><dd>{esc(LOCATION)}</dd>"
        "<dt>time type</dt><dd>Full time</dd>"
        "<dt>posted on</dt><dd>Posted 7 Days Ago</dd>"
        f"<dt>job requisition id</dt><dd>{esc(CODE)}</dd></dl>"
        f'<div data-automation-id="jobPostingDescription"><p>{COMPANY} builds forecasting tools for '
        "regional logistics networks. As a Growth Marketing Manager you will run lifecycle and "
        "paid programs with a small team.</p></div>"
    )


_ROUTES = (
    ("autofillWithResume", "Autofill with Resume"),
    ("applyManually", "Apply Manually"),
    ("useMyLastApplication", "Use My Last Application"),
)


def _route_links(origin: str) -> str:
    return "".join(
        f'<div><a href="{esc(origin)}/jobs/{SLUG}/apply/{route}" role="button" '
        f'data-automation-id="{route}" class="wd-button{"" if i == 0 else " wd-secondary"}">'
        f"{label}</a></div>"
        for i, (route, label) in enumerate(_ROUTES)
    )


def posting_html(origin: str) -> str:
    """The posting. Its Apply link, clicked, opens the "Start Your Application" popup in
    the page (the URL stays; the root turns aria-hidden), as on the live site; opened
    as a URL, ``<posting>/apply`` is a page of its own (``apply_page_html``)."""
    popup = (
        '<template id="wd-start-template"><div class="wd-glass" data-automation-id="wd-popup-glass">'
        '<div class="wd-dialog" data-automation-id="wd-popup-frame" role="dialog" '
        'aria-label="Start Your Application" tabindex="-1" data-automation-activepopup="true">'
        '<button class="wd-close" data-automation-id="closeButton" aria-label="Close" title="Close">'
        f"&times;</button><h2>Start Your Application</h2><h3>{esc(TITLE)}</h3>{_route_links(origin)}"
        "</div></div></template>"
        "<script>(function () {"
        "const apply = document.querySelector('[data-automation-id=adventureButton]');"
        "apply.addEventListener('click', (event) => {"
        "  event.preventDefault();"
        "  const glass = document.getElementById('wd-start-template').content.firstElementChild.cloneNode(true);"
        "  document.body.append(glass);"
        "  document.getElementById('root').setAttribute('aria-hidden', 'true');"
        "  glass.querySelector('[data-automation-id=closeButton]').addEventListener('click', () => {"
        "    glass.remove(); document.getElementById('root').removeAttribute('aria-hidden'); });"
        "  glass.querySelector('[data-automation-id=closeButton]').focus();"
        "});"
        "})();</script>"
    )
    return _document(TITLE, _posting_body(), origin=origin, glass=popup)


def apply_page_html(origin: str) -> str:
    """``<posting>/apply`` opened as a URL: the three routes on a page of their own
    (``applyAdventurePage``), as on the live site."""
    body = (
        '<div data-automation-id="applyAdventurePage">'
        f"<h2>Start Your Application</h2><h3>{esc(TITLE)}</h3>{_route_links(origin)}</div>"
    )
    return _document(f"{TITLE} - Start Your Application", body, origin=origin)


def _progress(current: int, *, done_through: int) -> str:
    """``current`` and ``done_through`` are 1-based step numbers."""
    total = len(STEP_TITLES)
    items = []
    for n, title in enumerate(STEP_TITLES, start=1):
        if n == current:
            aid, spoken = "progressBarActiveStep", f"current step {n} of {total}"
        elif n <= done_through:
            aid, spoken = "progressBarCompletedStep", f"completed step {n} of {total}"
        else:
            aid, spoken = "progressBarInactiveStep", f"step {n} of {total}"
        items.append(f'<li data-automation-id="{aid}"><label aria-live="polite" class="wd-sr">{spoken}</label>'
                     f"<label>{esc(title)}</label></li>")
    return ('<div aria-label="Application Progress"><ol data-automation-id="progressBar" '
            f'class="wd-progress">{"".join(items)}</ol></div>')


def _flow_head() -> str:
    return (
        '<div data-automation-id="applyFlowPage">'
        f'<button type="button" data-automation-id="backToJobPosting" role="link" '
        f"onclick=\"location.href='/jobs/{SLUG}'\">Back to Job Posting</button>"
        f'<h2 data-automation-id="jobTitleHeading">{esc(TITLE)}</h2>'
    )


_HONEYPOT = (
    '<div class="wd-honeypot"><label for="wd-beecatcher">Enter website. This input is for robots '
    "only, do not enter if you're human.</label>"
    '<input data-automation-id="beecatcher" id="wd-beecatcher" name="website" type="text"></div>'
)


def account_html(origin: str, *, mode: str, error: str | None = None, email: str = "") -> str:
    """The account step (signed out): Create Account, or Sign In after the toggle."""
    create = mode != "signin"
    title = "Create Account" if create else "Sign In"
    alert = (f'<div data-automation-id="errorMessage" role="alert" class="wd-banner">{esc(error)}</div>'
             if error else "")

    def text_field(fid: str, aid: str, label: str, kind: str, autocomplete: str, value: str = "") -> str:
        return (f'<div data-automation-id="formField-{aid}" class="wd-field"><label for="{fid}"><span>{label}'
                f'<abbr aria-hidden="true">*</abbr></span></label><div><input type="{kind}" '
                f'data-automation-id="{aid}" id="{fid}" name="{aid}" aria-required="true" aria-invalid="false" '
                f'autocomplete="{autocomplete}" value="{esc(value)}"></div></div>')

    fields = text_field("input-4", "email", "Email Address", "text", "email", email)
    fields += text_field("input-5", "password", "Password", "password",
                         "new-password" if create else "current-password")
    if create:
        fields += text_field("input-6", "verifyPassword", "Verify New Password", "password", "new-password")
        fields += (
            f"<p>By activating your account, you acknowledge and agree that you have read {COMPANY}'s "
            '<a href="/jobs/workday-wizard" target="_blank">Candidate Privacy Notice</a>.</p>'
            '<div data-automation-id="formField-" class="wd-field"><input id="input-9" type="checkbox" '
            'name="createAccountCheckbox" data-automation-id="createAccountCheckbox"> <label for="input-9">'
            f"I acknowledge and agree to {COMPANY}'s Candidate Privacy Notice</label></div>"
        )
    submit_aid = "createAccountSubmitButton" if create else "signInSubmitButton"
    form = (
        f'<form data-automation-id="signInFormo" method="post" action="/jobs/{SLUG}/account">'
        f'<input type="hidden" name="mode" value="{"create" if create else "signin"}">'
        f"{fields}"
        '<div data-automation-id="noCaptchaWrapper" style="position:relative">'
        f'<div aria-label="{title}" role="button" tabindex="0" data-automation-id="click_filter" '
        'class="wd-button" onclick="this.parentElement.querySelector(\'button\').click()">'
        f"{title}</div>"
        f'<button type="submit" data-automation-id="{submit_aid}" tabindex="-2" aria-hidden="true" '
        f'style="position:absolute;opacity:0;pointer-events:none;left:0;top:0">{title}</button></div></form>'
    )
    toggle = (
        '<div>Already have an account?<button data-automation-id="signInLink" '
        f"onclick=\"location.href='/jobs/{SLUG}/apply/applyManually?view=signin'\">Sign In</button></div>"
        if create else
        '<div>Don\'t have an account yet?<button data-automation-id="createAccountLink" '
        f"onclick=\"location.href='/jobs/{SLUG}/apply/applyManually'\">Create Account</button></div>"
    )
    rules = ("" if not create else
             '<div data-automation-id="passwordRulesList"><p>Password Requirements:</p><ul>'
             "<li>An uppercase character</li><li>A numeric character</li><li>A special character</li>"
             "<li>A minimum of 8 characters</li><li>A lowercase character</li></ul></div>")
    body = (
        _flow_head() + _progress(1, done_through=0)
        + f'<div data-automation-id="signInContent"><h3 id="authViewTitle" tabindex="-1">{title}</h3>'
        + alert + rules + form + toggle
        + '<div><button data-automation-id="forgotPasswordLink">Forgot your password?</button></div></div>'
        + _HONEYPOT + "</div>"
    )
    return _document(title, body, origin=origin)


def wizard_html(origin: str, *, email: str, saved: dict[str, Any], resume: dict[str, Any] | None) -> str:
    """The signed-in wizard: one document whose script renders each page in turn."""
    boot = {
        "pages": PAGES, "steps": STEP_TITLES, "email": email, "saved": saved,
        "resume": {"filename": resume["filename"], "upload_id": resume["upload_id"]} if resume else None,
        "slug": SLUG, "company": COMPANY,
    }
    body = (
        _flow_head()
        + '<div id="wd-progress"></div>'
        + '<span id="wd-announce" role="alert" class="wd-sr"></span>'
        + '<div id="wd-banner"></div>'
        + '<div id="wd-page" data-automation-id="applyFlowPage-content"></div>'
        + '<div class="wd-footer" data-automation-id="pageFooter">'
        + '<button type="button" data-automation-id="pageFooterBackButton" class="wd-button wd-secondary">Back</button>'
        + '<button type="button" data-automation-id="pageFooterNextButton" class="wd-button">Save and Continue</button>'
        + "</div>" + _HONEYPOT + "</div>"
        + "<script>window.__WD_BOOT = " + json.dumps(boot).replace("</", "<\\/") + ";</script>"
        + "<script>" + WIZARD_JS + "</script>"
    )
    return _document(f"{TITLE} - Application", body, origin=origin)


def submitted_html(origin: str, reference: str) -> str:
    body = (_flow_head() + f'<h3>Application Submitted</h3><p>Thank you for applying to {esc(TITLE)} '
            f"(Job ID {esc(CODE)}). Confirmation reference: {esc(reference)}</p></div>")
    return _document("Application Submitted", body, origin=origin)


WIZARD_JS = r"""(function () {
  const boot = window.__WD_BOOT;
  const pages = boot.pages;
  const state = { index: 0, saved: boot.saved || {}, values: {}, resume: boot.resume, done: 0 };
  const root = document.getElementById('wd-page');
  const next = document.querySelector('[data-automation-id=pageFooterNextButton]');
  const back = document.querySelector('[data-automation-id=pageFooterBackButton]');
  const banner = document.getElementById('wd-banner');
  const announce = document.getElementById('wd-announce');
  let popup = null;  // {owner, el, close}

  const h = (tag, attrs, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'text') el.textContent = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid);
    return el;
  };
  const labelEl = (f, forId) => h('label', {for: forId, id: f.id + '-label'},
    h('span', {}, f.label, f.required ? h('abbr', {'aria-hidden': 'true', text: '*'}) : null));
  const errorSlot = (f) => h('div', {id: f.id + '-error', 'data-automation-id': 'errorMessage', class: 'wd-error', hidden: true});

  // ---- popups (listbox portals) -----------------------------------------------------
  const onClose = new Map();
  const closePopup = () => {
    if (!popup) return;
    const {owner, el, aria} = popup;
    popup = null;
    el.remove();
    if (aria) { owner.removeAttribute('aria-expanded'); owner.removeAttribute('aria-controls'); }
    if (onClose.has(owner)) onClose.get(owner)();
  };
  const place = (owner, el) => {
    const r = owner.getBoundingClientRect();
    el.style.left = (r.left + window.scrollX) + 'px';
    el.style.top = (r.bottom + window.scrollY + 2) + 'px';
  };
  const openPopup = (owner, el, options = {aria: true}) => {
    closePopup();
    document.body.append(el);
    place(owner, el);
    popup = {owner, el, aria: options.aria};
    if (options.aria) {
      owner.setAttribute('aria-expanded', 'true');
      owner.setAttribute('aria-controls', el.querySelector('[role=listbox]') ? el.querySelector('[role=listbox]').id : el.id);
    }
  };
  document.addEventListener('mousedown', (event) => {
    if (!popup) return;
    if (popup.el.contains(event.target) || popup.owner.contains(event.target)) return;
    const wrap = popup.owner.closest('.wd-prompt');
    if (wrap && wrap.contains(event.target)) return;
    closePopup();
  });

  // ---- dropdown: <button aria-haspopup=listbox> (as on the live site) ---------------
  // Closed: no aria-expanded or aria-controls; a zero-size text input beside it keeps the
  // value. Open: aria-expanded=true and aria-controls name a body-portal ul[role=listbox]
  // (a new short id per open) whose first option is a disabled "Select One" with an empty
  // data-value; focus moves into the list; Escape or an outside press closes it.
  let listCounter = 0;
  const dropdown = (f, value) => {
    const labelOf = (v) => (f.options.find((o) => o[0] === v) || [null, ''])[1];
    const button = h('button', {type: 'button', id: f.id, name: f.name, class: 'wd-select',
      'aria-haspopup': 'listbox', 'data-automation-id': 'selectWidget'});
    const keep = h('input', {type: 'text', tabindex: '-1', 'aria-hidden': 'true',
      style: 'position:absolute;width:0;height:0;padding:0;border:0;opacity:0'});
    const show = () => {
      const v = state.values[f.name];
      button.textContent = v ? labelOf(v) : 'Select One';
      button.setAttribute('value', v || '');
      keep.value = v || '';
      button.setAttribute('aria-label', f.label + ' ' + (v ? labelOf(v) : 'Select One') + (f.required ? ' Required' : ''));
    };
    state.values[f.name] = value !== undefined ? value : (f.default || '');
    show();
    const choose = (v) => { state.values[f.name] = v; show(); closePopup(); clearError(f); button.focus(); };
    const open = () => {
      listCounter += 1;
      const current = state.values[f.name];
      const list = h('ul', {role: 'listbox', id: 'c' + (1000 + listCounter).toString(36) + 'q' + listCounter,
        tabindex: '-1', 'aria-required': f.required ? 'true' : null,
        'aria-activedescendant': current || 'select-one', class: 'wd-popup-list'},
        h('li', {role: 'option', id: 'select-one', 'data-value': '', 'aria-disabled': 'true'}, h('div', {text: 'Select One'})),
        f.options.map((o) => h('li', {role: 'option', id: o[0], 'data-value': o[0],
          'aria-selected': current === o[0] ? 'true' : null, onclick: () => choose(o[0])}, h('div', {text: o[1]}))));
      list.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { event.preventDefault(); closePopup(); button.focus(); }
      });
      openPopup(button, h('div', {class: 'wd-popup-host', style: 'position:absolute;z-index:40'}, list));
      list.style.position = 'static';
      list.focus();
    };
    button.addEventListener('click', () => { if (popup && popup.owner === button) closePopup(); else open(); });
    button.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { closePopup(); event.preventDefault(); }
      else if ((event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') && !(popup && popup.owner === button)) {
        open(); event.preventDefault();
      }
    });
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'},
      labelEl(f, f.id), h('div', {}, h('div', {style: 'position:relative'}, button, keep), errorSlot(f)));
  };

  // ---- prompt: Workday's search picker, as on the live site ---------------------------
  // The search box has no role, aria-haspopup or aria-controls; it is described by a
  // hidden "N items selected[, labels]" ("Expanded" while open). Chosen items are
  // role=option pills in a ul[role=listbox][aria-label="items selected"] beside it. A click
  // opens a body-portal div[role=listbox][aria-label="Options Expanded"] of categories;
  // typed words are searched on Enter; options' aria-labels end in "checked" / "not
  // checked". Escape does not close the list; an outside press does. A multi-select
  // picker keeps its list open after a choice; a single-select one replaces its item and
  // closes.
  let promptCounter = 0;
  const prompt = (f, value) => {
    promptCounter += 1;
    const leaves = f.tree ? f.tree.flatMap((g) => g.children) : f.options.map((o) => o[1]);
    const initial = Array.isArray(value) ? value : (value ? [value] : (f.default || []));
    state.values[f.name] = initial.slice();
    const instructionId = 'wd-instr-' + promptCounter;
    f.instruction = instructionId;
    const instruction = h('div', {id: instructionId, 'aria-hidden': 'true', 'data-automation-id': 'promptAriaInstruction',
      class: 'wd-sr'});
    const input = h('input', {id: f.id, enterkeyhint: 'search', placeholder: 'Search', 'aria-describedby': instructionId,
      'aria-required': f.required ? 'true' : null, 'aria-invalid': 'false', autocomplete: 'off', tabindex: '0',
      'data-uxi-widget-type': 'selectinput'});
    const chosenBox = h('div', {tabindex: '-1'});
    const wrap = h('div', {class: 'wd-prompt', 'data-automation-id': 'multiSelectContainer', tabindex: '-1'},
      h('div', {'data-automation-id': 'multiselectInputContainer', style: 'display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;flex:1'},
        h('div', {'data-automation-hiddensearch': 'false', style: 'flex:1;min-width:6rem'}, input,
          h('div', {'data-automation-id': 'promptSelectionLabel'}), instruction),
        chosenBox,
        h('span', {'data-automation-id': 'promptIcon', 'aria-hidden': 'true', text: '☰'})));
    let opened = false;
    const describe = () => {
      const chosen = state.values[f.name];
      const open = popup && popup.owner === input;
      opened = opened || !!open;
      // Live: "Expanded" while open; a single-select prompt then says "Minimized".
      instruction.textContent = open ? 'Expanded' : (f.single && opened) ? 'Minimized'
        : chosen.length + (chosen.length === 1 ? ' item' : ' items') + ' selected' + (chosen.length ? ', ' + chosen.join(', ') : '');
    };
    const renderChosen = () => {
      const chosen = state.values[f.name];
      chosenBox.replaceChildren(...(chosen.length ? [h('ul', {role: 'listbox', tabindex: '0', 'data-automation-id': 'selectedItemList',
        'aria-label': 'items selected', class: 'wd-pills'},
        chosen.map((item, i) => h('li', {role: 'presentation', 'data-automation-id': 'menuItem'},
          h('div', {role: 'option', id: 'pill-' + promptCounter + '-' + i, tabindex: '-1', 'data-automation-id': 'selectedItem',
            'aria-label': item + ', press delete to clear value.', 'aria-setsize': String(chosen.length), 'aria-posinset': String(i + 1)},
            h('div', {'data-automation-id': 'DELETE_charm', role: 'presentation', 'aria-hidden': 'true', class: 'wd-charm',
              onclick: () => { state.values[f.name] = state.values[f.name].filter((x) => x !== item); renderChosen(); describe(); }}),
            h('p', {'data-automation-id': 'promptOption', 'data-automation-label': item, text: item})))))] : []));
    };
    renderChosen();
    describe();
    let mode = {kind: 'browse', group: null, results: []};
    const choose = (leaf) => {
      const chosen = state.values[f.name];
      if (f.single) state.values[f.name] = [leaf];
      else state.values[f.name] = chosen.includes(leaf) ? chosen.filter((x) => x !== leaf) : [...chosen, leaf];
      renderChosen();
      input.value = '';
      clearError(f);
      if (f.single) { closePopup(); describe(); return; }
      render();
      describe();
      input.focus();
    };
    const row = (label, i, leaf, onclick) => {
      const checked = leaf && state.values[f.name].includes(label);
      return h('div', {role: 'option', id: 'menuItem-' + promptCounter + '-' + i, tabindex: '-1', 'data-automation-id': 'menuItem',
        'aria-label': label + (checked ? ' checked' : ' not checked'), 'aria-selected': checked ? 'true' : 'false',
        'aria-setsize': 'SETSIZE', 'aria-posinset': String(i + 1), onclick},
        h('div', {'data-automation-id': 'promptLeafNode', 'data-automation-checked': checked ? 'Checked' : 'Not Checked'},
          // A single-select prompt's rows carry a radio each (as on the live site).
          leaf && f.single ? h('input', {type: 'radio', id: 'radio-' + promptCounter + '-' + i, tabindex: '-1',
            checked: checked, 'aria-hidden': 'true', style: 'margin:0'}) : null,
          leaf && !f.single ? h('span', {class: 'wd-check', 'aria-hidden': 'true'}) : null,
          h('p', {'data-automation-id': 'promptOption', 'data-automation-label': label, text: label}),
          leaf ? null : h('span', {'aria-hidden': 'true', text: ' \u203a'})));
    };
    const render = () => {
      if (!popup || popup.owner !== input) return;
      const list = popup.el.querySelector('[role=listbox]');
      let kids = [];
      if (mode.kind === 'browse' && !mode.group && f.tree) {
        kids = f.tree.map((group, i) => row(group.label, i, false, () => { mode = {kind: 'browse', group, results: []}; render(); }));
      } else if (mode.kind === 'browse') {
        const items = mode.group ? mode.group.children : leaves;
        kids = items.map((leaf, i) => row(leaf, i, true, () => choose(leaf)));
      } else if (mode.kind === 'results') {
        kids = mode.results.map((leaf, i) => row(leaf, i, true, () => choose(leaf)));
      }
      for (const kid of kids) kid.setAttribute('aria-setsize', String(kids.length));
      if (kids.length) list.setAttribute('aria-activedescendant', kids[0].id); else list.removeAttribute('aria-activedescendant');
      list.replaceChildren(...kids);
    };
    const open = () => {
      if (popup && popup.owner === input) return;
      mode = {kind: 'browse', group: null, results: []};
      const list = h('div', {role: 'listbox', 'data-automation-id': 'activeListContainer', 'aria-label': 'Options Expanded',
        'aria-readonly': 'false', tabindex: '-1', class: 'wd-popup-list', style: 'position:static'});
      openPopup(input, h('div', {'data-automation-id': 'responsiveMonikerPrompt', class: 'wd-popup-host',
        style: 'position:absolute;z-index:40'}, list), {aria: false});
      render();
      describe();
    };
    input.addEventListener('click', open);
    input.addEventListener('input', () => {
      open();
      mode = input.value.trim() ? {kind: 'typing', group: null, results: []} : {kind: 'browse', group: null, results: []};
      render();
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        open();
        const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
        if (!words.length) { mode = {kind: 'browse', group: null, results: []}; render(); return; }
        // Searching takes a moment (a server search on the live site).
        setTimeout(() => {
          const results = leaves.filter((leaf) => {
            const parts = leaf.toLowerCase().split(/[^a-z0-9+]+/).filter(Boolean);
            return words.every((w) => parts.some((part) => part.startsWith(w.replace(/[^a-z0-9+]/g, ''))));
          });
          mode = {kind: 'results', group: null, results: [...new Set(results)]};
          render();
        }, 300);
      }
    });
    onClose.set(input, describe);
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'},
      labelEl(f, f.id), h('div', {}, wrap, errorSlot(f)));
  };

  // ---- date: Month / Day / Year spinbuttons ------------------------------------------
  const dateField = (f, value) => {
    const parts = (value || '').split('/');
    const spec = [['month', 'Month', 'MM', 2, 1, 12], ['day', 'Day', 'DD', 2, 1, 31], ['year', 'Year', 'YYYY', 4, 1900, 2100]];
    const inputs = spec.map(([kind, name, shown, size, min, max], i) => h('input', {type: 'text', role: 'spinbutton',
      id: i === 0 ? f.id + '-dateSectionMonth-input' : f.id + '-dateSection' + name + '-input',
      'data-automation-id': 'dateSection' + name + '-input', 'aria-label': name, 'aria-valuemin': String(min),
      'aria-valuemax': String(max), 'aria-valuetext': shown, 'aria-required': f.required ? 'true' : null,
      inputmode: 'numeric', autocomplete: 'off', placeholder: shown, value: parts[i] || ''}));
    const sizes = spec.map((s) => s[3]);
    const sync = () => { state.values[f.name] = inputs.some((x) => x.value) ? inputs.map((x) => x.value).join('/') : ''; };
    inputs.forEach((input, i) => {
      input.addEventListener('keydown', (event) => {
        if (/^\d$/.test(event.key)) {
          event.preventDefault();
          const start = input.selectionStart, end = input.selectionEnd;
          let text = (start !== end) ? event.key : (input.value + event.key);
          if (text.length > sizes[i]) text = event.key;
          input.value = text;
          input.setAttribute('aria-valuenow', text);
          input.setAttribute('aria-valuetext', text);
          sync();
          clearError(f);
          if (text.length >= sizes[i] && inputs[i + 1]) { inputs[i + 1].focus(); inputs[i + 1].select(); }
        } else if (event.key === '/' || event.key === '-' || event.key === '.') {
          event.preventDefault();
          if (input.value && inputs[i + 1]) { inputs[i + 1].focus(); inputs[i + 1].select(); }
        } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
          event.preventDefault();
        }
      });
      input.addEventListener('input', sync);
    });
    sync();
    const calendar = h('button', {type: 'button', 'data-automation-id': 'dateIcon', 'aria-label': 'Calendar', title: 'Calendar'}, '📅');
    const wrapper = h('div', {class: 'wd-date', 'data-automation-id': 'dateInputWrapper'},
      inputs[0], h('span', {'aria-hidden': 'true', text: '/'}), inputs[1], h('span', {'aria-hidden': 'true', text: '/'}), inputs[2], calendar);
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'},
      labelEl(f, inputs[0].id), h('div', {}, wrapper, errorSlot(f)));
  };

  // ---- file: drop zone, sent on attach, input emptied ---------------------------------
  const fileField = (f) => {
    const input = h('input', {type: 'file', id: f.id + '-input', 'data-automation-id': 'file-upload-input-ref',
      accept: f.accept, 'aria-required': f.required ? 'true' : null, class: 'wd-sr'});
    const items = h('div', {'data-automation-id': 'file-upload-successful'});
    const renderItems = () => {
      items.replaceChildren(...(state.resume ? [h('div', {class: 'wd-file-item', 'data-automation-id': 'file-upload-item'},
        h('span', {'data-automation-id': 'file-upload-item-name', text: state.resume.filename}),
        h('button', {type: 'button', 'data-automation-id': 'delete-file', 'aria-label': 'Delete ' + state.resume.filename,
          onclick: () => { state.resume = null; renderItems(); }}, 'Delete'))] : []));
    };
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const body = new FormData();
      body.append('file', file, file.name);
      const response = await fetch('/jobs/' + boot.slug + '/wizard/upload', {method: 'POST', body});
      const data = await response.json();
      if (response.ok) {
        state.resume = {filename: data.filename, upload_id: data.upload_id};
        clearError(f);
      }
      input.value = '';
      renderItems();
    });
    renderItems();
    const group = h('div', {role: 'group', 'aria-labelledby': f.id + '-group-label', 'data-automation-id': 'attachments-FileUpload'},
      h('h4', {id: f.id + '-group-label'}, f.label, f.required ? h('abbr', {'aria-hidden': 'true', text: '*'}) : null),
      h('div', {class: 'wd-drop', 'data-automation-id': 'file-upload-drop-zone'},
        h('p', {text: 'Drop file here'}), h('p', {text: 'or'}),
        h('label', {for: input.id, class: 'wd-button', 'data-automation-id': 'select-files', text: 'Select files'}), input),
      items, h('div', {id: input.id + '-error', 'data-automation-id': 'errorMessage', class: 'wd-error', hidden: true}));
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'}, group);
  };

  // ---- native fields --------------------------------------------------------------------
  const textField = (f, value) => {
    const input = h('input', {type: 'text', id: f.id, name: f.name, 'data-automation-id': f.name,
      'aria-required': f.required ? 'true' : null, 'aria-invalid': 'false', autocomplete: 'off'});
    input.value = value !== undefined ? value : (f.prefill === 'account_email' ? boot.email : '');
    state.values[f.name] = input.value;
    input.addEventListener('input', () => { state.values[f.name] = input.value; clearError(f); });
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'},
      labelEl(f, f.id), h('div', {}, input, errorSlot(f)));
  };
  const radioField = (f, value) => {
    state.values[f.name] = value || '';
    const fieldset = h('fieldset', {id: f.id, 'aria-describedby': null},
      h('legend', {}, f.label, f.required ? h('abbr', {'aria-hidden': 'true', text: '*'}) : null),
      f.options.map((o, i) => h('div', {class: 'wd-choice'},
        h('input', {type: 'radio', name: f.name, id: f.id + '-' + i, value: o[0], 'aria-required': f.required ? 'true' : null,
          checked: state.values[f.name] === o[0], onchange: () => { state.values[f.name] = o[0]; clearError(f); }}),
        h('label', {for: f.id + '-' + i, text: o[1]}))));
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'}, fieldset, errorSlot(f));
  };
  const checkboxField = (f, value) => {
    state.values[f.name] = value === true;
    const input = h('input', {type: 'checkbox', id: f.id, name: f.name, 'aria-required': f.required ? 'true' : null,
      checked: value === true, onchange: () => { state.values[f.name] = input.checked; clearError(f); }});
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'},
      f.terms ? h('p', {text: f.terms}) : null,
      h('div', {}, input, ' ', h('label', {for: f.id}, f.label, f.required ? h('abbr', {'aria-hidden': 'true', text: '*'}) : null)),
      errorSlot(f));
  };
  const checkgroupField = (f, value) => {
    state.values[f.name] = Array.isArray(value) ? value.slice() : [];
    const boxes = [];
    const fieldset = h('fieldset', {id: f.id, 'aria-describedby': f.id + '-hint'},
      h('legend', {}, f.label, f.required ? h('abbr', {'aria-hidden': 'true', text: '*'}) : null),
      h('p', {id: f.id + '-hint', text: f.hint || ''}),
      f.options.map((o, i) => {
        const box = h('input', {type: 'checkbox', name: f.name, id: f.id + '-' + i, value: o[0],
          checked: state.values[f.name].includes(o[0]),
          onchange: () => {
            // One box at a time, as Workday's self-identification form behaves.
            if (box.checked) for (const other of boxes) if (other !== box) other.checked = false;
            state.values[f.name] = boxes.filter((b) => b.checked).map((b) => b.value);
            clearError(f);
          }});
        boxes.push(box);
        return h('div', {class: 'wd-choice'}, box, h('label', {for: f.id + '-' + i, text: o[1]}));
      }));
    return h('div', {'data-automation-id': 'formField-' + f.name, class: 'wd-field'}, fieldset, errorSlot(f));
  };
  const addSection = (f) => h('div', {class: 'wd-field', 'data-automation-id': f.name + 'Section'},
    h('h3', {text: f.label}),
    h('button', {type: 'button', class: 'wd-button wd-secondary', 'data-automation-id': 'Add',
      'aria-label': 'Add ' + f.label, onclick: () => {}}, 'Add'));

  // ---- errors ------------------------------------------------------------------------------
  const targetOf = (f) => {
    if (f.kind === 'radio' || f.kind === 'checkgroup') return document.getElementById(f.id);
    if (f.kind === 'date') return document.getElementById(f.id + '-dateSectionMonth-input');
    if (f.kind === 'file') return document.getElementById(f.id + '-input');
    return document.getElementById(f.id);
  };
  const slotOf = (f) => document.getElementById((f.kind === 'file' ? f.id + '-input' : f.id) + '-error');
  // What a control is described by without an error (a group's own hint).
  const baseDescribedBy = (f) => (f.kind === 'checkgroup' ? f.id + '-hint' : f.kind === 'prompt' ? f.instruction : null);
  function clearError(f) {
    const target = targetOf(f), slot = slotOf(f);
    if (target) {
      target.removeAttribute('aria-invalid');
      if (baseDescribedBy(f)) target.setAttribute('aria-describedby', baseDescribedBy(f));
      else target.removeAttribute('aria-describedby');
    }
    if (slot) { slot.hidden = true; slot.textContent = ''; }
  }
  const showErrors = (page, errors) => {
    const entries = page.fields.filter((f) => errors[f.name]);
    for (const f of page.fields) if (f.kind !== 'add_section') clearError(f);
    banner.replaceChildren();
    if (!entries.length) return;
    for (const f of entries) {
      const target = targetOf(f), slot = slotOf(f);
      slot.textContent = 'Error: ' + errors[f.name];
      slot.hidden = false;
      if (target) {
        target.setAttribute('aria-invalid', 'true');
        const ids = baseDescribedBy(f) ? baseDescribedBy(f) + ' ' + slot.id : slot.id;
        target.setAttribute('aria-describedby', ids);
      }
    }
    banner.append(h('div', {'data-automation-id': 'errorBanner', role: 'alert', class: 'wd-banner'},
      h('button', {type: 'button', 'data-automation-id': 'errorsFoundButton', text: 'Errors Found (' + entries.length + ')'}),
      h('ul', {}, entries.map((f) => h('li', {text: 'Error - ' + f.label + ': ' + errors[f.name]})))));
  };

  // ---- pages -------------------------------------------------------------------------------
  const progress = () => {
    const total = boot.steps.length, current = state.index + 2;
    const done = Math.max(1, state.done + 1);
    document.getElementById('wd-progress').replaceChildren(h('div', {'aria-label': 'Application Progress'},
      h('ol', {'data-automation-id': 'progressBar', class: 'wd-progress'}, boot.steps.map((title, i) => {
        const n = i + 1;
        const [aid, spoken] = n === current ? ['progressBarActiveStep', 'current step ' + n + ' of ' + total]
          : n <= done ? ['progressBarCompletedStep', 'completed step ' + n + ' of ' + total]
          : ['progressBarInactiveStep', 'step ' + n + ' of ' + total];
        return h('li', {'data-automation-id': aid}, h('label', {'aria-live': 'polite', class: 'wd-sr', text: spoken}), h('label', {text: title}));
      }))));
  };
  const reviewPage = () => {
    const sections = pages.map((page) => h('section', {'data-automation-id': 'reviewSection-' + page.id},
      h('h3', {text: page.title}),
      h('dl', {class: 'wd-review'}, page.fields.filter((f) => f.kind !== 'add_section').map((f) => {
        const saved = state.saved[page.id] || {};
        let shown = '';
        if (f.kind === 'file') shown = state.resume ? state.resume.filename : '';
        else if (f.kind === 'dropdown' || f.kind === 'radio') shown = ((f.options.find((o) => o[0] === saved[f.name]) || [null, ''])[1]);
        else if (f.kind === 'checkgroup') shown = (saved[f.name] || []).map((v) => (f.options.find((o) => o[0] === v) || [null, v])[1]).join(', ');
        else if (f.kind === 'prompt') shown = (saved[f.name] || []).join(', ');
        else if (f.kind === 'checkbox') shown = saved[f.name] === true ? 'Yes' : '';
        else shown = saved[f.name] || '';
        return [h('dt', {text: f.label}), h('dd', {text: shown})];
      }))));
    return h('div', {'data-automation-id': 'reviewPage'}, h('h2', {text: 'Review'}), ...sections);
  };
  const render = () => {
    closePopup();
    banner.replaceChildren();
    progress();
    state.values = {};
    const review = state.index >= pages.length;
    const title = review ? 'Review' : pages[state.index].title;
    if (review) {
      root.replaceChildren(reviewPage());
      next.textContent = 'Submit';
      next.setAttribute('data-automation-id', 'pageFooterSubmitButton');
    } else {
      const page = pages[state.index];
      const saved = state.saved[page.id] || {};
      const kids = [h('h2', {text: page.title})];
      let section = null;
      for (const f of page.fields) {
        if (f.section && f.section !== section) { kids.push(h('h3', {text: f.section})); section = f.section; }
        const value = saved[f.name];
        kids.push({dropdown, prompt, date: dateField, file: fileField, text: textField, radio: radioField,
          checkbox: checkboxField, checkgroup: checkgroupField, add_section: addSection}[f.kind](f, value));
      }
      root.replaceChildren(h('div', {'data-automation-id': 'applyFlowPage-' + page.id}, kids));
      next.textContent = 'Save and Continue';
      next.setAttribute('data-automation-id', 'pageFooterNextButton');
    }
    back.hidden = state.index === 0;
    announce.textContent = title + ' page is loaded';
    window.scrollTo(0, 0);
  };
  const post = async (path, payload) => {
    const response = await fetch('/jobs/' + boot.slug + '/wizard/' + path, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    return {ok: response.ok, data: await response.json()};
  };
  next.addEventListener('click', async () => {
    closePopup();
    if (state.index >= pages.length) {
      const {ok, data} = await post('submit', {});
      if (ok) { location.href = '/jobs/' + boot.slug + '/wizard/submitted?ref=' + encodeURIComponent(data.reference); }
      else banner.replaceChildren(h('div', {role: 'alert', class: 'wd-banner', text: data.error || 'Submit failed.'}));
      return;
    }
    const page = pages[state.index];
    const values = Object.assign({}, state.values);
    const {ok, data} = await post('save', {page: page.id, values});
    if (!ok) { showErrors(page, data.errors || {}); return; }
    state.saved[page.id] = values;
    state.index += 1;
    state.done = Math.max(state.done, state.index);
    render();
  });
  back.addEventListener('click', () => {
    if (state.index > 0) { state.index -= 1; render(); }
  });
  window.__wdState = state;
  render();
})();"""
