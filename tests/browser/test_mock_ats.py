"""Standard-library HTTP tests for the localhost mock ATS (scripts/mock_ats.py).

Forms are filled by their visible labels and encoded the way a browser submits
a native HTML form, so the fixture is verified without a product-specific
field resolver. Server-side outcomes are asserted through the test-only API.

Run:  uv run --no-project --python 3.12 python -m unittest discover -s tests/browser -p 'test_mock_ats.py' -v
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import uuid
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "mock_ats.py"
FIXTURES = REPO / "tests" / "fixtures" / "browser"
CANDIDATE = json.loads((FIXTURES / "candidate.json").read_text("utf-8"))
USER_INPUTS = json.loads((FIXTURES / "user_inputs.json").read_text("utf-8"))
RESUME_PATH = FIXTURES / CANDIDATE["resume"]["path"]
RESUME_BYTES = RESUME_PATH.read_bytes()
RESUME_SHA256 = hashlib.sha256(RESUME_BYTES).hexdigest()


def _load_mock_ats():
    spec = importlib.util.spec_from_file_location("mock_ats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mock_ats"] = module
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no scripts/__pycache__
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


mock_ats = _load_mock_ats()


# --------------------------------------------------------------------------
# Minimal browser-like helpers
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    text = " ".join(text.split())
    return re.sub(r"\s*\(optional\)$", "", text)


class Control:
    def __init__(self, tag: str, attrs: dict[str, str | None], legend: str | None):
        self.tag = tag
        self.attrs = attrs
        self.type = (attrs.get("type") or "text").lower() if tag == "input" else tag
        self.name = attrs.get("name")
        self.id = attrs.get("id")
        self.value = attrs.get("value") or ""
        self.checked = "checked" in attrs
        self.required = "required" in attrs
        self.multiple = "multiple" in attrs
        self.options: list[list[Any]] = []  # [value, label, selected]
        self.legend = legend
        self.file: tuple[str, str, bytes] | None = None


class Page(HTMLParser):
    """Parse a response into text, labels and forms (like a tiny DOM)."""

    def __init__(self, document: str):
        super().__init__(convert_charrefs=True)
        self.forms: list[NativeForm] = []
        self.labels: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self.images: list[dict[str, str | None]] = []
        self.ld_json: list[Any] = []
        self._text: list[str] = []
        self._form: NativeForm | None = None
        self._legends: list[str | None] = []
        self._legend_buf: list[str] | None = None
        self._label: tuple[str, list[str]] | None = None
        self._link: tuple[str, list[str]] | None = None
        self._select: Control | None = None
        self._option: list[Any] | None = None
        self._textarea: Control | None = None
        self._spans: list[bool] = []
        self._script: list[str] | None = None
        self.feed(document)
        self.text = _norm(" ".join(self._text))

    @property
    def form(self) -> NativeForm:
        return self.forms[0]

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag == "span":
            self._spans.append(attrs.get("aria-hidden") == "true")
        elif tag == "form":
            self._form = NativeForm(
                attrs.get("action") or "",
                (attrs.get("method") or "get").upper(),
                attrs.get("enctype") or "application/x-www-form-urlencoded",
                self.labels,
            )
            self.forms.append(self._form)
        elif tag == "fieldset":
            self._legends.append(None)
        elif tag == "legend":
            self._legend_buf = []
        elif tag == "label":
            self._label = (attrs.get("for") or "", [])
        elif tag == "a":
            self._link = (attrs.get("href") or "", [])
        elif tag == "img":
            self.images.append(attrs)
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self._script = []
        elif tag in ("input", "select", "textarea") and self._form is not None:
            control = Control(tag, attrs, self._legends[-1] if self._legends else None)
            self._form.controls.append(control)
            if tag == "select":
                self._select = control
            elif tag == "textarea":
                self._textarea = control
        elif tag == "option" and self._select is not None:
            self._option = [attrs.get("value") or "", [], "selected" in attrs]

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._spans:
            self._spans.pop()
        elif tag == "form":
            self._form = None
        elif tag == "fieldset" and self._legends:
            self._legends.pop()
        elif tag == "legend" and self._legend_buf is not None:
            if self._legends:
                self._legends[-1] = _norm("".join(self._legend_buf))
            self._legend_buf = None
        elif tag == "label" and self._label is not None:
            self.labels[self._label[0]] = _norm("".join(self._label[1]))
            self._label = None
        elif tag == "a" and self._link is not None:
            self.links.append((self._link[0], _norm("".join(self._link[1]))))
            self._link = None
        elif tag == "script" and self._script is not None:
            self.ld_json.append(json.loads("".join(self._script)))
            self._script = None
        elif tag == "option" and self._option is not None and self._select is not None:
            value, label, selected = self._option
            self._select.options.append([value, _norm("".join(label)), selected])
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
            return
        if any(self._spans):
            return
        self._text.append(data)
        for buf in (
            self._legend_buf,
            self._label[1] if self._label else None,
            self._link[1] if self._link else None,
            self._option[1] if self._option else None,
        ):
            if buf is not None:
                buf.append(data)
        if self._textarea is not None:
            self._textarea.value += data


class NativeForm:
    """Fill controls by visible label and encode like a browser submission."""

    def __init__(self, action: str, method: str, enctype: str, labels: dict[str, str]):
        self.action = action
        self.method = method
        self.enctype = enctype
        self.labels = labels
        self.controls: list[Control] = []

    def label_of(self, control: Control) -> str:
        return self.labels.get(control.id or "", "")

    def by_label(self, label: str) -> Control:
        matches = [c for c in self.controls if c.id and self.label_of(c) == _norm(label)]
        if len(matches) != 1:
            raise AssertionError(f"expected one control labelled {label!r}, found {len(matches)}")
        return matches[0]

    def by_name(self, name: str) -> list[Control]:
        return [c for c in self.controls if c.name == name]

    def hidden(self, name: str) -> str:
        return next(c.value for c in self.by_name(name) if c.type == "hidden")

    def fill(self, label: str, value: str) -> None:
        control = self.by_label(label)
        assert control.type in ("text", "email", "tel", "url", "password", "textarea"), control.type
        control.value = value

    def select(self, label: str, *option_labels: str) -> None:
        control = self.by_label(label)
        assert control.tag == "select"
        known = {opt[1] for opt in control.options}
        missing = set(option_labels) - known
        assert not missing, f"no options {missing} in {label!r}"
        assert control.multiple or len(option_labels) == 1
        for opt in control.options:
            opt[2] = opt[1] in option_labels

    def choose(self, legend: str, option_label: str, checked: bool = True) -> None:
        group = [c for c in self.controls if c.legend == _norm(legend)]
        target = [c for c in group if self.label_of(c) == _norm(option_label)]
        assert len(target) == 1, f"no choice {option_label!r} under {legend!r}"
        if target[0].type == "radio":
            for c in group:
                if c.name == target[0].name:
                    c.checked = False
        target[0].checked = checked

    def check(self, label: str, checked: bool = True) -> None:
        control = self.by_label(label)
        assert control.type == "checkbox"
        control.checked = checked

    def attach(self, label: str, filename: str, data: bytes, content_type: str) -> None:
        control = self.by_label(label)
        assert control.type == "file"
        control.file = (filename, content_type, data)

    def entries(self) -> list[tuple]:
        items: list[tuple] = []
        for c in self.controls:
            if not c.name or c.type in ("submit", "button", "reset", "image"):
                continue
            if c.type in ("checkbox", "radio"):
                if c.checked:
                    items.append(("field", c.name, c.value or "on"))
            elif c.tag == "select":
                chosen = [o[0] for o in c.options if o[2]]
                if not chosen and not c.multiple and c.options:
                    chosen = [c.options[0][0]]
                items.extend(("field", c.name, v) for v in chosen)
            elif c.type == "file":
                name, ctype, data = c.file or ("", "application/octet-stream", b"")
                items.append(("file", c.name, name, ctype, data))
            else:
                items.append(("field", c.name, c.value))
        return items

    def encode(self) -> tuple[bytes, str]:
        items = self.entries()
        if self.enctype != "multipart/form-data":
            # Browsers send only the file name for file inputs in urlencoded forms.
            pairs = [(i[1], i[2]) for i in items]
            return urlencode(pairs).encode("ascii"), "application/x-www-form-urlencoded"
        return encode_multipart(items)


def encode_multipart(items: list[tuple]) -> tuple[bytes, str]:
    boundary = "----MockAtsTestBoundary" + uuid.uuid4().hex
    out = bytearray()
    for item in items:
        out += f"--{boundary}\r\n".encode()
        if item[0] == "field":
            _, name, value = item
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            out += value.encode("utf-8") + b"\r\n"
        else:
            _, name, filename, ctype, data = item
            safe = filename.replace('"', "%22")
            out += (
                f'Content-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class Response:
    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes, path: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.path = path

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    @property
    def page(self) -> Page:
        return Page(self.text)

    def json(self) -> Any:
        return json.loads(self.body)


class Client:
    """Cookie-keeping HTTP client that follows 303 redirects like a browser."""

    def __init__(self, origin: str):
        split = urlsplit(origin)
        self.host, self.port = split.hostname, split.port
        self.cookies: dict[str, str] = {}

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: dict | None = None
    ) -> Response:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = dict(headers or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if method == "POST" and body is None:
            body = b""
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        for header in resp.headers.get_all("Set-Cookie") or []:
            cookie = SimpleCookie(header)
            self.cookies.update({k: m.value for k, m in cookie.items()})
        conn.close()
        return Response(resp.status, resp.headers, data, path)

    def follow(self, resp: Response) -> Response:
        while resp.status in (301, 302, 303, 307, 308):
            resp = self.request("GET", urljoin(resp.path, resp.headers["Location"]))
        return resp

    def get(self, path: str, follow: bool = True) -> Response:
        resp = self.request("GET", path)
        return self.follow(resp) if follow else resp

    def submit(self, form: NativeForm, follow: bool = True) -> Response:
        body, ctype = form.encode()
        resp = self.request("POST", form.action, body, {"Content-Type": ctype})
        return self.follow(resp) if follow else resp

    def post_form(self, path: str, fields: list[tuple[str, str]], follow: bool = False) -> Response:
        resp = self.request(
            "POST",
            path,
            urlencode(fields).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self.follow(resp) if follow else resp

    def api(self, method: str, path: str) -> Any:
        resp = self.request(method, path)
        assert resp.status == 200, (resp.status, resp.text)
        return resp.json()


def fill_core(form: NativeForm, phone: str | None = None) -> None:
    """Fill the common fields from the fictional candidate fixture, by label."""
    ident = CANDIDATE["identity"]
    form.fill("First name", ident["first_name"])
    form.fill("Last name", ident["last_name"])
    form.fill("Email", ident["email"])
    form.fill("Phone", phone or ident["phone"])
    form.fill("LinkedIn profile URL", ident["linkedin_url"])
    form.attach("Resume", RESUME_PATH.name, RESUME_BYTES, "application/pdf")
    form.select(
        "Are you legally authorized to work in the United States?",
        "Yes, I am authorized to work in the US",
    )
    form.choose(
        "Will you now or in the future require visa sponsorship?",
        "No, I will not require sponsorship",
    )


def fill_extended(form: NativeForm) -> None:
    form.select("Years of professional experience", "6 to 9 years")
    form.select("Primary skills", *CANDIDATE["facts"]["skills"])
    for arrangement in CANDIDATE["facts"]["work_arrangements"]:
        form.choose("Which work arrangements would you consider?", arrangement)
    form.fill(
        "Why do you want to work at Brambleway Analytics?",
        CANDIDATE["saved_answers"]["why_brambleway"],
    )


CORE_EXPECTED = {
    "first_name": "Avery",
    "last_name": "Quill",
    "email": "avery.quill@example.test",
    "phone": "+1 (303) 555-0142",
    "linkedin_url": "https://www.linkedin.example.test/in/avery-quill",
    "work_authorization": "wa_authorized",
    "sponsorship": "no_sponsorship",
}


# --------------------------------------------------------------------------
# In-process server tests
# --------------------------------------------------------------------------


class MockATSTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="mock-ats-test-")
        cls.ats = mock_ats.MockATS(port=0, state_dir=Path(cls._tmp.name) / "state").start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ats.stop()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.client = Client(self.ats.origin)
        self.assertEqual(self.client.api("POST", "/__test__/reset"), {"reset": True})

    def counts(self, job_id: str | None = None) -> dict[str, Any]:
        query = f"?job_id={job_id}" if job_id else ""
        return self.client.api("GET", f"/__test__/submissions{query}")

    def open_form(self, job_id: str) -> Page:
        resp = self.client.get(f"/jobs/{job_id}/apply")
        self.assertEqual(resp.status, 200, resp.text)
        return resp.page

    def assert_rejected(self, resp: Response, *labels: str) -> Page:
        self.assertEqual(resp.status, 422)
        page = resp.page
        self.assertIn("There is a problem with your application", page.text)
        self.assertIn('role="alert"', resp.text)
        for label in labels:
            self.assertIn(_norm(label), page.text)
        return page

    def assert_confirmed(self, resp: Response) -> str:
        self.assertEqual(resp.status, 200, resp.text)
        self.assertRegex(resp.path, r"^/applications/sub_\d{6}$")
        match = re.search(r"Confirmation reference: (BWA-\d{6})", resp.page.text)
        self.assertIsNotNone(match, resp.page.text)
        self.assertIn("Application submitted", resp.page.text)
        return match.group(1)


class SiteAndMarkupTests(MockATSTestCase):
    def test_origin_is_loopback_with_ephemeral_port(self) -> None:
        self.assertRegex(self.ats.origin, r"^http://127\.0\.0\.1:\d+$")
        self.assertNotEqual(self.ats.port, 0)
        health = self.client.api("GET", "/__test__/health")
        self.assertEqual(health["origin"], self.ats.origin)

    def test_non_loopback_hosts_are_refused(self) -> None:
        for host in ("0.0.0.0", "192.168.1.10", "example.test", "::1"):
            with self.assertRaises(ValueError, msg=host):
                mock_ats.MockATS(host=host, port=0, state_dir=Path(self._tmp.name) / "x")

    def test_index_and_job_identity(self) -> None:
        index = self.client.get("/").page
        hrefs = {href for href, _ in index.links}
        catalog = self.client.api("GET", "/__test__/jobs")
        for job in catalog["jobs"]:
            self.assertIn(job["posting_path"], hrefs)
        posting = self.client.get("/jobs/standard")
        page = posting.page
        self.assertIn("Senior Data Platform Engineer", page.text)
        self.assertIn("Job ID BWA-ENG-101", page.text)
        self.assertIn(("/jobs/standard/apply", "Apply for this job"), page.links)
        (ld,) = page.ld_json
        self.assertEqual(ld["@type"], "JobPosting")
        self.assertEqual(ld["identifier"]["value"], "BWA-ENG-101")
        self.assertEqual(ld["hiringOrganization"]["name"], "Brambleway Analytics")
        self.assertIn(f'href="{self.ats.origin}/jobs/standard"', posting.text)
        self.assertEqual(self.client.get("/jobs/nope").status, 404)

    def test_standard_form_is_accessible_native_html(self) -> None:
        form = self.open_form("standard").form
        self.assertEqual(form.method, "POST")
        self.assertEqual(form.enctype, "multipart/form-data")
        kinds = {c.type for c in form.controls}
        self.assertTrue(
            {"text", "email", "tel", "url", "file", "textarea", "select", "radio", "checkbox"}
            <= kinds,
            kinds,
        )
        self.assertTrue(any(c.multiple for c in form.controls if c.tag == "select"))
        for c in form.controls:
            if c.type == "hidden":
                continue
            self.assertTrue(form.label_of(c), f"control {c.name} has no label")
            if c.type in ("radio",) or (c.type == "checkbox" and c.name == "work_arrangements"):
                self.assertTrue(c.legend, f"{c.name} is not in a labelled fieldset")
            if c.tag == "select":
                for value, label, _ in c.options:
                    if value:
                        self.assertNotEqual(value, label)
        required = {c.name for c in form.controls if c.required}
        self.assertTrue({"first_name", "email", "resume", "skills", "sponsorship"} <= required)
        self.assertNotIn("linkedin_url", required)
        self.assertNotIn("open_to_relocation", required)

    def test_every_job_form_labels_every_visible_control(self) -> None:
        catalog = self.client.api("GET", "/__test__/jobs")
        self.client.post_form(
            "/login",
            [("email", catalog["signin"]["email"]), ("password", catalog["signin"]["password"])],
        )
        for job in catalog["jobs"]:
            if job["formless"]:
                continue  # rendered without a <form> on purpose (a Rippling-style SPA)
            form = self.open_form(job["job_id"]).form
            for c in form.controls:
                if c.type != "hidden":
                    self.assertTrue(form.label_of(c), f"{job['job_id']}: {c.name} unlabelled")

    def test_unknown_routes_and_methods(self) -> None:
        self.assertEqual(self.client.get("/nowhere").status, 404)
        self.assertEqual(self.client.get("/favicon.ico").status, 204)
        self.assertEqual(self.client.request("POST", "/").status, 405)
        self.assertEqual(self.client.get("/applications/sub_999999").status, 404)
        self.assertEqual(self.client.request("GET", "/__test__/submissions/sub_999999").status, 404)

    def test_oversized_body_is_refused_before_reading(self) -> None:
        conn = http.client.HTTPConnection(self.client.host, self.client.port, timeout=10)
        conn.putrequest("POST", "/jobs/standard/apply")
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.putheader("Content-Length", str(mock_ats.MAX_BODY_BYTES + 1))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()
        self.assertEqual(self.counts()["accepted_count"], 0)


class StandardSubmissionTests(MockATSTestCase):
    def test_accepted_with_visible_confirmation_and_exact_record(self) -> None:
        form = self.open_form("standard").form
        fill_core(form)
        fill_extended(form)
        resp = self.client.submit(form)
        reference = self.assert_confirmed(resp)
        self.assertIn("Thank you, Avery", resp.page.text)
        self.assertIn("Job ID BWA-ENG-101", resp.page.text)

        summary = self.counts("standard")
        self.assertEqual((summary["accepted_count"], summary["rejected_count"]), (1, 0))
        (record,) = summary["submissions"]
        self.assertEqual(record["confirmation_reference"], reference)
        self.assertEqual(resp.path, f"/applications/{record['submission_id']}")
        self.assertTrue(record["confirmation_visible"])
        self.assertEqual(
            record["fields"],
            CORE_EXPECTED
            | {
                "years_experience": "yrs_6_9",
                "skills": ["sk_python", "sk_sql", "sk_spark", "sk_dbt"],
                "work_arrangements": ["arr_remote", "arr_hybrid"],
                "why_brambleway": CANDIDATE["saved_answers"]["why_brambleway"],
            },
        )
        self.assertNotIn("open_to_relocation", record["fields"])
        self.assertEqual(record["extra_fields"], {})
        resume = record["files"]["resume"]
        self.assertEqual(resume["filename"], "resume_avery_quill.pdf")
        self.assertEqual(resume["content_type"], "application/pdf")
        self.assertEqual(resume["size"], len(RESUME_BYTES))
        self.assertEqual(resume["sha256"], RESUME_SHA256)
        self.assertEqual(Path(resume["stored_path"]).read_bytes(), RESUME_BYTES)
        self.assertEqual(self.client.api("GET", f"/__test__/submissions/{record['submission_id']}"), record)

    def test_single_checkbox_value_is_recorded(self) -> None:
        form = self.open_form("standard").form
        fill_core(form)
        fill_extended(form)
        form.check("I am open to relocating to Denver, CO")
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("standard")["submissions"]
        self.assertEqual(record["fields"]["open_to_relocation"], "yes")

    def test_binary_upload_and_unicode_filename_round_trip(self) -> None:
        # Every byte value plus CRLF and boundary-like sequences.
        data = bytes(range(256)) * 4 + b"\r\n--\r\n------MockAtsTestBoundary\r\n\r\n" + b"\x00" * 7
        form = self.open_form("standard").form
        fill_core(form)
        fill_extended(form)
        form.attach("Resume", "résumé Ω.txt", data, "text/plain")
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("standard")["submissions"]
        meta = record["files"]["resume"]
        self.assertEqual(meta["filename"], "résumé Ω.txt")
        self.assertEqual(meta["size"], len(data))
        self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())

    def test_missing_resume_and_answers_are_visibly_rejected_and_not_counted(self) -> None:
        form = self.open_form("standard").form
        form.fill("First name", "Avery")
        resp = self.client.submit(form)
        page = self.assert_rejected(
            resp,
            "Resume: Attach a file.",
            "Last name: This field is required.",
            "Primary skills: Select at least one option.",
            "Will you now or in the future require visa sponsorship?: Select an answer.",
        )
        self.assertIn('aria-invalid="true"', resp.text)
        self.assertEqual(page.form.by_label("First name").value, "Avery")
        summary = self.counts("standard")
        self.assertEqual((summary["accepted_count"], summary["rejected_count"]), (0, 1))
        self.assertIn("resume", summary["rejections"][0]["errors"])

    def test_labels_instead_of_machine_values_are_rejected(self) -> None:
        form = self.open_form("standard").form
        fill_core(form)
        fill_extended(form)
        items = [
            ("field", "work_authorization", "Yes, I am authorized to work in the US")
            if i[:2] == ("field", "work_authorization")
            else i
            for i in form.entries()
        ]
        body, ctype = encode_multipart(items)
        resp = self.client.request("POST", "/jobs/standard/apply", body, {"Content-Type": ctype})
        self.assert_rejected(
            resp,
            "Are you legally authorized to work in the United States?: "
            "Select one of the listed options.",
        )
        self.assertEqual(self.counts()["accepted_count"], 0)

    def test_every_accepted_post_is_counted_so_retries_are_detectable(self) -> None:
        form = self.open_form("standard").form
        fill_core(form)
        fill_extended(form)
        first = self.assert_confirmed(self.client.submit(form))
        second = self.assert_confirmed(self.client.submit(form))
        self.assertNotEqual(first, second)
        summary = self.counts("standard")
        self.assertEqual(summary["accepted_count"], 2)
        self.assertEqual(
            [s["confirmation_reference"] for s in summary["submissions"]], [first, second]
        )
        status = self.client.get(
            "/jobs/standard/application-status?email=avery.quill%40example.test"
        ).page.text
        self.assertIn(first, status)
        self.assertIn(second, status)

    def test_undeclared_fields_are_recorded_separately(self) -> None:
        form = self.open_form("standard").form
        fill_core(form)
        fill_extended(form)
        body, ctype = encode_multipart([*form.entries(), ("field", "surprise", "value")])
        resp = self.client.follow(
            self.client.request("POST", "/jobs/standard/apply", body, {"Content-Type": ctype})
        )
        self.assert_confirmed(resp)
        (record,) = self.counts()["submissions"]
        self.assertEqual(record["extra_fields"], {"surprise": "value"})


class ScenarioTests(MockATSTestCase):
    def test_missing_required_answers_block_until_user_supplies_them(self) -> None:
        form = self.open_form("missing-required").form
        fill_core(form)
        resp = self.client.submit(form)
        page = self.assert_rejected(
            resp,
            "What is your notice period?: Select an answer.",
            "Desired annual base salary (USD): This field is required.",
            "Do you hold an active FAA Part 107 remote pilot certificate?: Select an answer.",
        )
        self.assertEqual(self.counts("missing-required")["accepted_count"], 0)
        errors = self.counts("missing-required")["rejections"][0]["errors"]
        self.assertEqual(set(errors), {"notice_period", "salary_expectation", "faa_part_107"})

        form = page.form  # resume retained, other values preserved
        self.assertEqual(form.by_label("Email").value, CANDIDATE["identity"]["email"])
        answers = USER_INPUTS["missing-required"]
        form.select("What is your notice period?", answers["What is your notice period?"])
        form.fill("Desired annual base salary (USD)", answers["Desired annual base salary (USD)"])
        faa = "Do you hold an active FAA Part 107 remote pilot certificate?"
        form.choose(faa, answers[faa])
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("missing-required")["submissions"]
        self.assertEqual(record["fields"]["notice_period"], "notice_2w")
        self.assertEqual(record["fields"]["salary_expectation"], "150000")
        self.assertEqual(record["fields"]["faa_part_107"], "faa_no")
        self.assertEqual(record["files"]["resume"]["sha256"], RESUME_SHA256)

    def test_changed_after_prepare_gains_a_required_question_from_its_second_load(self) -> None:
        travel = "Are you willing to travel to client sites up to 25% of the time?"
        first = self.open_form("changed-after-prepare").form
        self.assertNotIn("travel_willingness", {c.name for c in first.controls})
        second = self.open_form("changed-after-prepare").form
        [control, _no] = second.by_name("travel_willingness")
        self.assertTrue(control.required)
        # The server validates the form it served last: the first form's answers alone
        # are rejected now, and nothing is counted.
        fill_core(first)
        page = self.assert_rejected(self.client.submit(first), f"{travel}: Select an answer.")
        self.assertEqual(self.counts("changed-after-prepare")["accepted_count"], 0)
        form = page.form
        form.choose(travel, "Yes")
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("changed-after-prepare")["submissions"]
        self.assertEqual(record["fields"]["travel_willingness"], "travel_yes")
        self.assertEqual(record["extra_fields"], {})
        catalog = {job["job_id"]: job for job in self.client.api("GET", "/__test__/jobs")["jobs"]}
        self.assertEqual(catalog["changed-after-prepare"]["added_on_reload"]["name"],
                         "travel_willingness")
        # A reset starts the load count again.
        self.client.api("POST", "/__test__/reset")
        form = self.open_form("changed-after-prepare").form
        self.assertNotIn("travel_willingness", {c.name for c in form.controls})
        fill_core(form)
        self.assert_confirmed(self.client.submit(form))

    def test_attestations_are_required_unchecked_checkboxes(self) -> None:
        form = self.open_form("attestation").form
        labels = list(USER_INPUTS["attestation"])
        for label in labels:
            control = form.by_label(label)
            self.assertEqual(control.type, "checkbox")
            self.assertTrue(control.required)
            self.assertFalse(control.checked)
        fill_core(form)
        resp = self.client.submit(form)
        page = self.assert_rejected(resp, *(f"{label}: Check this box to continue." for label in labels))
        self.assertEqual(self.counts("attestation")["accepted_count"], 0)

        form = page.form
        for label in labels:
            form.check(label)
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("attestation")["submissions"]
        self.assertEqual(record["fields"]["attest_accuracy"], "yes")
        self.assertEqual(record["fields"]["attest_privacy_notice"], "yes")

    def test_validation_rejection_is_visible_and_keeps_the_upload(self) -> None:
        form = self.open_form("validation").form
        fill_core(form)
        resp = self.client.submit(form)
        page = self.assert_rejected(resp, f"Phone: {mock_ats.US_PHONE_MESSAGE}")
        self.assertRegex(resp.text, r'id="f-phone"[^>]*aria-invalid="true"')
        self.assertIn(
            "Currently attached: resume_avery_quill.pdf", page.text
        )
        summary = self.counts("validation")
        self.assertEqual((summary["accepted_count"], summary["rejected_count"]), (0, 1))
        self.assertEqual(summary["rejections"][0]["errors"], {"phone": mock_ats.US_PHONE_MESSAGE})

        form = page.form
        self.assertEqual(form.by_label("Phone").value, CANDIDATE["identity"]["phone"])
        self.assertFalse(form.by_label("Resume").required)
        upload_id = form.hidden("resume_upload_id")
        form.fill("Phone", USER_INPUTS["validation"]["Phone"])
        self.assert_confirmed(self.client.submit(form))  # no new file attached
        (record,) = self.counts("validation")["submissions"]
        self.assertEqual(record["fields"]["phone"], "3035550142")
        self.assertEqual(record["files"]["resume"]["upload_id"], upload_id)
        self.assertEqual(record["files"]["resume"]["sha256"], RESUME_SHA256)

    def test_signin_is_required_before_the_form(self) -> None:
        first = self.client.get("/jobs/signin/apply", follow=False)
        self.assertEqual(first.status, 303)
        self.assertEqual(first.headers["Location"], "/login?next=/jobs/signin/apply")
        blind = self.client.post_form("/jobs/signin/apply", [("first_name", "Avery")])
        self.assertEqual(blind.status, 303)
        self.assertEqual(self.counts("signin")["accepted_count"], 0)

        login = self.client.follow(first)
        self.assertIn("Sign in to continue your application", login.page.text)
        form = login.page.form
        form.fill("Email", USER_INPUTS["signin"]["email"])
        form.fill("Password", "wrong-password")
        bad = self.client.submit(form)
        self.assertEqual(bad.status, 401)
        self.assertIn("Incorrect email or password.", bad.page.text)
        self.assertEqual(self.client.cookies, {})

        form = bad.page.form
        form.fill("Password", USER_INPUTS["signin"]["password"])
        resp = self.client.submit(form)
        self.assertEqual(resp.path, "/jobs/signin/apply")
        self.assertIn(mock_ats.SESSION_COOKIE, self.client.cookies)
        apply_form = resp.page.form
        fill_core(apply_form)
        self.assert_confirmed(self.client.submit(apply_form))
        self.assertEqual(self.counts("signin")["accepted_count"], 1)

    def test_captcha_needs_a_person_and_is_single_use(self) -> None:
        page = self.open_form("captcha")
        self.assertIn("Verify you are human (CAPTCHA)", page.text)
        (image,) = page.images
        token = page.form.hidden("captcha_token")
        self.assertEqual(image["src"], f"/captcha/{token}.svg")
        svg = self.client.get(image["src"])
        self.assertEqual(svg.headers["Content-Type"], "image/svg+xml")
        answer = self.client.api("GET", f"/__test__/captcha/{token}")["answer"]
        self.assertRegex(answer, r"^[A-Z2-9]{5}$")
        self.assertNotIn(answer, page.text)

        form = page.form
        fill_core(form)
        form.fill("Characters shown in the image", "WRONG")
        resp = self.client.submit(form)
        rejected = self.assert_rejected(
            resp, "Characters shown in the image: The characters did not match."
        )
        self.assertEqual(self.counts("captcha")["accepted_count"], 0)

        # The spent challenge cannot be replayed, even with the right answer.
        replay = [
            ("field", n, answer) if n == "captcha_answer" else i
            for i in form.entries()
            for n in [i[1]]
        ]
        body, ctype = encode_multipart(replay)
        again = self.client.request("POST", "/jobs/captcha/apply", body, {"Content-Type": ctype})
        self.assertEqual(again.status, 422)

        form = rejected.form
        new_token = form.hidden("captcha_token")
        self.assertNotEqual(new_token, token)
        new_answer = self.client.api("GET", f"/__test__/captcha/{new_token}")["answer"]
        form.fill("Characters shown in the image", new_answer.lower())
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("captcha")["submissions"]
        self.assertNotIn("captcha_answer", record["fields"])
        self.assertEqual(record["extra_fields"], {})

    def test_uncertain_submission_is_counted_then_revealed_through_the_page(self) -> None:
        form = self.open_form("uncertain").form
        fill_core(form)
        resp = self.client.submit(form)
        self.assertEqual(resp.status, 502)
        self.assertIn("Something went wrong", resp.page.text)
        self.assertNotRegex(resp.page.text, r"BWA-\d{6}|submitted|received")

        # Counted before the confirmation was withheld.
        summary = self.counts("uncertain")
        self.assertEqual(summary["accepted_count"], 1)
        (record,) = summary["submissions"]
        self.assertFalse(record["confirmation_visible"])
        self.assertEqual(record["files"]["resume"]["sha256"], RESUME_SHA256)
        reference = record["confirmation_reference"]
        self.assertEqual(self.client.get(f"/applications/{record['submission_id']}").status, 404)

        # Reconciliation through the public status page, before the reveal.
        posting = self.client.get("/jobs/uncertain").page
        status_link = dict((text, href) for href, text in posting.links)[
            "Already applied? Check your application status"
        ]
        status_form = self.client.get(status_link).page.form
        status_form.fill("Email used on your application", CANDIDATE["identity"]["email"])
        body, _ = status_form.encode()
        status_url = f"{status_form.action}?{body.decode()}"
        pending = self.client.get(status_url).page.text
        self.assertIn("still processing a recent application", pending)
        self.assertNotIn(reference, pending)

        # A mistaken retry is accepted and counted again, so it is detectable.
        self.assertEqual(self.client.submit(form).status, 502)
        self.assertEqual(self.counts("uncertain")["accepted_count"], 2)

        # Test-only reveal; the runtime then sees a real receipt on a later visit.
        revealed = self.client.api("POST", f"/__test__/submissions/{record['submission_id']}/reveal")
        self.assertTrue(revealed["confirmation_visible"])
        self.assertIsNotNone(revealed["revealed_at"])
        later = self.client.get(status_url).page
        self.assertIn(f"Confirmation reference: {reference}", later.text)
        self.assertIn("still processing", later.text)  # the unrevealed retry
        confirmation_href = dict((t, h) for h, t in later.links)["View confirmation"]
        self.assertEqual(self.assert_confirmed(self.client.get(confirmation_href)), reference)

    def test_status_page_for_unknown_email(self) -> None:
        text = self.client.get(
            "/jobs/standard/application-status?email=nobody%40example.test"
        ).page.text
        self.assertIn("could not find an application", text)


class MultistepTests(MockATSTestCase):
    def test_multistep_navigation_review_and_submit(self) -> None:
        page = self.open_form("multistep")
        self.assertIn("Step 1 of 4", page.text)
        self.assertIn('aria-current="step"', self.client.get("/jobs/multistep/apply").text)
        form = page.form
        self.assertEqual(form.enctype, "application/x-www-form-urlencoded")
        self.assertEqual({c.name for c in form.controls if c.type == "file"}, set())

        # Step 1 rejection does not create a draft.
        rejected = self.client.submit(form, follow=False)
        self.assert_rejected(rejected, "First name: This field is required.")
        ident = CANDIDATE["identity"]
        form = rejected.page.form
        for label, key in (
            ("First name", "first_name"),
            ("Last name", "last_name"),
            ("Email", "email"),
            ("Phone", "phone"),
            ("LinkedIn profile URL", "linkedin_url"),
        ):
            form.fill(label, ident[key])
        step1 = self.client.submit(form, follow=False)
        self.assertEqual(step1.status, 303)
        match = re.fullmatch(r"/jobs/multistep/apply/(dft_\d{6})/step/2", step1.headers["Location"])
        self.assertIsNotNone(match)
        base = f"/jobs/multistep/apply/{match.group(1)}"

        # Steps cannot be skipped, and review is unavailable while incomplete.
        for path in (f"{base}/step/3", f"{base}/review"):
            skip = self.client.get(path, follow=False)
            self.assertEqual((skip.status, skip.headers["Location"]), (303, f"{base}/step/2"))
        early = self.client.request("POST", f"{base}/submit")
        self.assertEqual((early.status, early.headers["Location"]), (303, f"{base}/step/2"))

        step2 = self.client.follow(step1)
        self.assertIn("Step 2 of 4", step2.page.text)
        form = step2.page.form
        self.assertEqual(form.enctype, "multipart/form-data")
        self.assert_rejected(self.client.submit(form), "Resume: Attach a file.")
        form.attach("Resume", RESUME_PATH.name, RESUME_BYTES, "application/pdf")
        form.select("Years of professional experience", "6 to 9 years")
        form.select(
            "Are you legally authorized to work in the United States?",
            "Yes, I am authorized to work in the US",
        )
        form.choose(
            "Will you now or in the future require visa sponsorship?",
            "No, I will not require sponsorship",
        )
        step3 = self.client.submit(form)
        self.assertEqual(step3.path, f"{base}/step/3")

        # Back to step 1 shows saved values; returning to step 2 keeps the resume.
        back = dict((t, h) for h, t in step3.page.links)["Back"]
        self.assertEqual(back, f"{base}/step/2")
        revisit = self.client.get(f"{base}/step/1").page.form
        self.assertEqual(revisit.by_label("Email").value, ident["email"])
        revisit2 = self.client.get(back).page
        self.assertIn("Currently attached: resume_avery_quill.pdf", revisit2.text)
        self.assertFalse(revisit2.form.by_label("Resume").required)

        form = step3.page.form
        fill_extended_step3(form)
        review = self.client.submit(form)
        self.assertEqual(review.path, f"{base}/review")
        text = review.page.text
        self.assertIn("Step 4 of 4", text)
        for expected in (
            "Avery",
            "6 to 9 years",
            "Yes, I am authorized to work in the US",
            "No, I will not require sponsorship",
            "Python, SQL, Apache Spark, dbt",
            "Remote, Hybrid",
            f"resume_avery_quill.pdf ({len(RESUME_BYTES)} bytes)",
        ):
            self.assertIn(expected, text)
        self.assertIn("I am open to relocating to Denver, CO No", text)
        self.assertNotIn("sk_python", text)
        self.assertEqual(self.counts("multistep")["accepted_count"], 0)

        reference = self.assert_confirmed(self.client.submit(review.page.form))
        summary = self.counts("multistep")
        self.assertEqual(summary["accepted_count"], 1)
        (record,) = summary["submissions"]
        self.assertEqual(record["confirmation_reference"], reference)
        self.assertEqual(record["draft_id"], match.group(1))
        self.assertEqual(
            record["fields"],
            CORE_EXPECTED
            | {
                "years_experience": "yrs_6_9",
                "skills": ["sk_python", "sk_sql", "sk_spark", "sk_dbt"],
                "work_arrangements": ["arr_remote", "arr_hybrid"],
                "why_brambleway": CANDIDATE["saved_answers"]["why_brambleway"],
            },
        )
        self.assertEqual(record["files"]["resume"]["sha256"], RESUME_SHA256)

        # Re-posting the review form is another accepted POST and is counted.
        self.assert_confirmed(self.client.submit(review.page.form))
        self.assertEqual(self.counts("multistep")["accepted_count"], 2)

    def test_unknown_draft_is_not_found(self) -> None:
        self.assertEqual(self.client.get("/jobs/multistep/apply/dft_999999/step/1").status, 404)
        self.assertEqual(self.client.get("/jobs/standard/apply/dft_000001/step/1").status, 404)


def fill_extended_step3(form: NativeForm) -> None:
    form.select("Primary skills", *CANDIDATE["facts"]["skills"])
    for arrangement in CANDIDATE["facts"]["work_arrangements"]:
        form.choose("Which work arrangements would you consider?", arrangement)
    form.fill(
        "Why do you want to work at Brambleway Analytics?",
        CANDIDATE["saved_answers"]["why_brambleway"],
    )


class BrowserRuntimeScenarioTests(MockATSTestCase):
    """Scenarios added for the C4 browser runtime."""

    def test_disabled_option_is_shown_but_rejected(self) -> None:
        form = self.open_form("missing-required").form
        notice = form.by_label("What is your notice period?")
        self.assertIn(
            ["notice_3m_plus", "3 months or more (no longer offered)", False], notice.options
        )
        self.assertIn('value="notice_3m_plus" disabled', self.client.get("/jobs/missing-required/apply").text)
        fill_core(form)
        form.fill("Desired annual base salary (USD)", "150000")
        form.choose("Do you hold an active FAA Part 107 remote pilot certificate?", "No")
        notice.options[-1][2] = True  # force-post the disabled value
        self.assert_rejected(
            self.client.submit(form), "What is your notice period?: Select one of the listed options."
        )
        self.assertEqual(self.counts()["accepted_count"], 0)

    def test_custom_combobox_and_honeypot(self) -> None:
        response = self.client.get("/jobs/custom-control/apply")
        self.assertIn('role="combobox"', response.text)
        self.assertIn('aria-required="true"', response.text)
        self.assertRegex(response.text, r'<input type="text" id="f-referral_code"[^>]* disabled')
        form = response.page.form
        fill_core(form)
        self.assert_rejected(self.client.submit(form), "Preferred office: Select an answer.")

        form = self.open_form("custom-control").form
        fill_core(form)
        next(c for c in form.by_name("preferred_office") if c.type == "hidden").value = "office_den"
        form.fill("Leave this field blank", "https://spam.example.test")
        self.client.submit(form)
        rejection = self.counts("custom-control")["rejections"][-1]
        self.assertIn("website_hp", rejection["errors"])

        form = self.open_form("custom-control").form
        fill_core(form)
        next(c for c in form.by_name("preferred_office") if c.type == "hidden").value = "office_den"
        self.assert_confirmed(self.client.submit(form))
        (record,) = self.counts("custom-control")["submissions"]
        self.assertEqual(record["fields"]["preferred_office"], "office_den")
        self.assertNotIn("referral_code", record["fields"])

    def test_vague_confirmation_names_nothing_but_status_page_confirms(self) -> None:
        form = self.open_form("vague-confirmation").form
        fill_core(form)
        response = self.client.submit(form)
        self.assertEqual(response.status, 200)
        self.assertIn("Thank you!", response.page.text)
        self.assertNotRegex(response.page.text, r"BWA-|QA Engineer|submitted|received")
        (record,) = self.counts("vague-confirmation")["submissions"]
        status = self.client.get(
            "/jobs/vague-confirmation/application-status?email=avery.quill%40example.test"
        ).page.text
        self.assertIn(record["confirmation_reference"], status)

    def test_agreement_checkboxes_have_terms_outside_the_label(self) -> None:
        response = self.client.get("/jobs/agreement/apply")
        page = response.page
        agree = [c for c in page.form.controls if page.form.label_of(c) == "I agree"]
        self.assertEqual([c.name for c in agree], ["agree_declaration", "agree_retention"])
        self.assertTrue(all(c.required for c in agree))
        self.assertIn("<legend>Candidate declaration</legend>", response.text)
        self.assertIn('<p class="terms">I confirm that I have never been dismissed', response.text)
        self.assertIn('aria-describedby="f-agree_retention-hint"', response.text)

    def test_sign_in_cookie_persists(self) -> None:
        creds = self.client.api("GET", "/__test__/jobs")["signin"]
        response = self.client.post_form(
            "/login", [("email", creds["email"]), ("password", creds["password"]), ("next", "/")]
        )
        self.assertIn("Max-Age=86400", response.headers["Set-Cookie"])


class PersistenceTests(unittest.TestCase):
    def test_state_survives_restart_and_ids_continue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mock-ats-test-") as tmp:
            state = Path(tmp) / "state"
            with mock_ats.MockATS(port=0, state_dir=state) as ats:
                client = Client(ats.origin)
                form = client.get("/jobs/uncertain/apply").page.form
                fill_core(form)
                self.assertEqual(client.submit(form).status, 502)
            with mock_ats.MockATS(port=0, state_dir=state) as ats:
                client = Client(ats.origin)
                summary = client.api("GET", "/__test__/submissions")
                self.assertEqual(summary["accepted_count"], 1)
                self.assertEqual(summary["submissions"][0]["submission_id"], "sub_000001")
                form = client.get("/jobs/standard/apply").page.form
                fill_core(form)
                fill_extended(form)
                resp = client.submit(form)
                self.assertIn("BWA-000002", resp.page.text)


# --------------------------------------------------------------------------
# Command-line process tests
# --------------------------------------------------------------------------


class CommandLineTests(unittest.TestCase):
    def start(self, *args: str) -> tuple[subprocess.Popen, dict[str, str]]:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._cleanup, proc)
        info: dict[str, str] = {}
        while len(info) < 3:
            line = proc.stdout.readline()
            if not line:
                self.fail(f"server exited early: {proc.stderr.read()}")
            key, _, value = line.strip().partition("=")
            info[key] = value
        return proc, info

    @staticmethod
    def _cleanup(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    def test_prints_origin_writes_ready_file_and_stops_on_sigterm(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mock-ats-test-") as tmp:
            ready = Path(tmp) / "ready.json"
            state = Path(tmp) / "state"
            proc, info = self.start("--port", "0", "--state-dir", str(state), "--ready-file", str(ready))
            origin = info["MOCK_ATS_ORIGIN"]
            self.assertRegex(origin, r"^http://127\.0\.0\.1:\d+$")
            self.assertEqual(Path(info["MOCK_ATS_STATE_DIR"]), state.resolve())
            self.assertEqual(int(info["MOCK_ATS_PID"]), proc.pid)
            self.assertEqual(
                json.loads(ready.read_text()),
                {"origin": origin, "state_dir": str(state.resolve()), "pid": proc.pid},
            )
            self.assertTrue(Client(origin).api("GET", "/__test__/health")["ok"])
            self.assertTrue((state / "state.json").exists())

            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=10), 0)
            self.assertIn("MOCK_ATS_STOPPED", proc.stdout.read())
            self.assertFalse(ready.exists())

    def test_test_shutdown_endpoint_stops_only_this_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mock-ats-test-") as tmp:
            proc, info = self.start("--state-dir", tmp)
            self.assertEqual(
                Client(info["MOCK_ATS_ORIGIN"]).api("POST", "/__test__/shutdown"),
                {"stopping": True},
            )
            self.assertEqual(proc.wait(timeout=10), 0)

    def test_default_state_dir_is_a_fresh_temporary_directory(self) -> None:
        proc, info = self.start()
        state = Path(info["MOCK_ATS_STATE_DIR"])
        self.addCleanup(shutil.rmtree, state, True)
        self.assertTrue(state.name.startswith("mock-ats-"))
        self.assertNotIn(str(REPO), str(state))
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_refuses_non_loopback_host(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mock-ats-test-") as tmp:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--host", "0.0.0.0", "--state-dir", tmp],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("loopback", result.stderr)


if __name__ == "__main__":
    unittest.main()
