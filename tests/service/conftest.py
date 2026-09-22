"""Service test harness: a running loopback server over the real canonical store.

``FakeCandidates`` stands in for the C2P candidate API and ``ScriptedRunner`` for the
I1 runner. The scripted runner is not a second executor: it performs the exact store
operations the runner recipe (CONTRACTS.md §7) prescribes, against a fictional site
whose behaviour each test chooses, so the service is exercised against real canonical
state, events, claims and submission guards. Final S1 acceptance additionally runs the
real I1 runner and browser (``test_acceptance.py``).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from interviewmaxxing_core import (
    AnswerSource,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    ApplyOutcome,
    CandidateIdentity,
    CandidateProfile,
    EvidenceKind,
    EvidenceRef,
    LocalPaths,
    MissingInput,
    MissingReason,
    PacketAnswer,
    Provenance,
    ReconciliationMethod,
    ResumeArtifact,
    SavedAnswer,
    SubmissionObservation,
    SubmissionOutcome,
    SubmissionReconciliation,
)
from interviewmaxxing_service import (
    Dispatcher,
    PresentationService,
    ResumeEntry,
    ServiceConfig,
    ServiceInteraction,
    make_server,
)
from interviewmaxxing_service.candidate import CandidateSetupError

ORIGIN = "http://127.0.0.1:4317"
SITE_URL = "https://jobs.example.test/fictional-co/4012/apply?src=desk"
S = ApplicationState


# --- candidate package stand-in ----------------------------------------------------------


class FakeCandidates:
    """In-memory ``CandidateGateway`` with real resume files under a private dir."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.resumes: dict[str, dict[str, ResumeEntry]] = {}
        self.profiles: dict[str, CandidateProfile] = {}
        self.saved_answers: list[SavedAnswer] = []
        self.upserts = 0

    def load_profile(self, candidate_id: str) -> CandidateProfile | None:
        return self.profiles.get(candidate_id)

    def list_resumes(self, candidate_id: str) -> list[ResumeEntry]:
        with self.lock:
            entries = list(self.resumes.get(candidate_id, {}).values())
        return sorted(entries, key=lambda e: e.uploaded_at, reverse=True)

    def store_resume(
        self, candidate_id: str, *, filename: str, content: bytes, media_type: str
    ) -> ResumeEntry:
        if b"REJECT" in content:
            raise CandidateSetupError("resumeFile", "The candidate store rejected this file.")
        digest = hashlib.sha256(content).hexdigest()
        rid = f"res_{digest[:16]}_{len(self.resumes.get(candidate_id, {}))}"
        path = self.root / candidate_id / rid
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        entry = ResumeEntry(
            id=rid, filename=filename, size_bytes=len(content),
            uploaded_at=datetime.now(UTC), sha256=digest, media_type=media_type,
        )
        with self.lock:
            self.resumes.setdefault(candidate_id, {})[rid] = entry
        return entry

    def upsert_profile(
        self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str
    ) -> CandidateProfile:
        entry = self.resumes[candidate_id][resume_id]
        resume = ResumeArtifact(
            id=entry.id, path=str(self.root / candidate_id / entry.id), filename=entry.filename,
            media_type=entry.media_type, sha256=entry.sha256, size_bytes=entry.size_bytes,
        )
        current = self.profiles.get(candidate_id)
        profile = (
            current.model_copy(update={"identity": identity, "resume": resume})
            if current
            else CandidateProfile(id=candidate_id, identity=identity, resume=resume)
        )
        self.profiles[candidate_id] = profile
        self.upserts += 1
        return profile

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        self.saved_answers.append(answer)


# --- scripted fictional site + runner -------------------------------------------------------


@dataclass
class FictionalSite:
    """What the fictional site does. Tests flip these switches."""

    form: ApplicationForm
    outcome: SubmissionOutcome = SubmissionOutcome.ACCEPTED
    revealed: bool = False
    sign_in_first: bool = False
    crash_before_submit: bool = False
    accepted_posts: int = 0
    hold: threading.Event = field(default_factory=threading.Event)
    holding: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.hold.set()


class ScriptedRunner:
    """Performs the runner recipe's store operations against ``FictionalSite``."""

    owner = "scripted-runner"

    def __init__(self, paths: LocalPaths, site: FictionalSite, interaction: ServiceInteraction):
        self.paths = paths
        self.site = site
        self.interaction = interaction

    def _outcome(self, store: ApplicationStore, app_id: str, message: str = "") -> ApplyOutcome:
        app = store.get_application(app_id)
        return ApplyOutcome(
            application_id=app_id, state=app.state, receipt=store.get_receipt(app_id),
            message=message,
        )

    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.paths.state_db) as store:
            result = store.record_request(candidate_id, application_url)
            if not result.may_proceed:
                return self._outcome(store, result.application.id)
            return await self._work(store, result.application.id)

    async def resume(self, application_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.paths.state_db) as store:
            return await self._work(store, application_id)

    async def reconcile(self, application_id: str) -> ApplyOutcome:
        with ApplicationStore.open(self.paths.state_db) as store:
            if self.site.revealed:
                claim = store.claim(application_id, self.owner)
                store.reconcile_submission(claim, SubmissionReconciliation(
                    outcome=SubmissionOutcome.ACCEPTED,
                    method=ReconciliationMethod.SITE_CONFIRMATION,
                    detail="confirmation page shows reference FIC-000001",
                    confirmation_reference="FIC-000001",
                ))
                store.release(claim)
            return self._outcome(store, application_id)

    async def _work(self, store: ApplicationStore, app_id: str) -> ApplyOutcome:
        claim = store.claim(app_id, self.owner)
        try:
            self.site.holding.set()
            self.site.hold.wait(10)
            store.transition(claim, S.INSPECTING)
            if self.site.sign_in_first and not self.interaction.allow_browser_action:
                store.transition(
                    claim, S.NEEDS_INPUT,
                    metadata={"page_kind": "SIGN_IN_REQUIRED",
                              "observed_url": "https://jobs.example.test/sign-in"},
                )
                return self._outcome(store, app_id)
            form = self.site.form
            app = store.get_application(app_id)
            inputs = {u.field_id: u for u in store.get_user_inputs(app_id, form)}
            answers, missing = [], []
            for f in form.fields:
                if f.id in inputs:
                    answers.append(PacketAnswer(
                        field_id=f.id, semantic_type=f.semantic_type, value=inputs[f.id].value,
                        provenance=Provenance(
                            source=AnswerSource.USER_INPUT, reference_ids=[inputs[f.id].id]
                        ),
                    ))
                else:
                    reason = (
                        MissingReason.EXPLICIT_ANSWER_REQUIRED
                        if f.semantic_type.value in ("EEO_GENDER", "CONSENT")
                        else MissingReason.NO_ANSWER
                    )
                    missing.append(MissingInput.for_field(
                        form, f, reason=reason, prompt=f"Please answer: {f.label}"
                    ))
            packet = ApplicationPacket(
                application_id=app_id, job_id=app.job_id, candidate_id=app.candidate_id,
                form_url=form.url, form_step=form.step, form_fingerprint=form.fingerprint,
                answers=answers, missing_inputs=missing,
            )
            assert packet.problems_against(form) == []
            store.save_packet(claim, packet)
            if not packet.is_complete:
                store.transition(claim, S.NEEDS_INPUT)
                return self._outcome(store, app_id)
            store.transition(claim, S.PACKET_READY)
            store.transition(claim, S.FILLING)
            if self.site.crash_before_submit:
                raise RuntimeError("fictional browser crashed")
            attempt = store.begin_submission(claim, packet_id=packet.id)
            self.site.accepted_posts += 1
            evidence_dir = self.paths.application_artifacts(app_id)
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "confirmation.png").write_bytes(b"\x89PNG fictional")
            (evidence_dir / "page.html").write_bytes(b"<script>alert(1)</script>")
            evidence = [
                EvidenceRef(kind=EvidenceKind.SCREENSHOT, path=f"{app_id}/confirmation.png",
                            description="Confirmation page"),
                EvidenceRef(kind=EvidenceKind.HTML_SNAPSHOT, path=f"{app_id}/page.html"),
            ]
            if self.site.outcome is SubmissionOutcome.ACCEPTED:
                observation = SubmissionObservation(
                    outcome=SubmissionOutcome.ACCEPTED,
                    signals=["heading 'Application received'"],
                    confirmation_reference="FIC-000001", evidence=evidence,
                )
            else:
                observation = SubmissionObservation(
                    outcome=SubmissionOutcome.UNKNOWN, detail="no confirmation shown",
                    evidence=evidence,
                )
            store.record_submission_outcome(claim, attempt.id, observation)
            return self._outcome(store, app_id)
        finally:
            self.site.holding.clear()
            store.release(claim)


# --- HTTP client -----------------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        *,
        host: str | None = None,
        origin: str | None = ORIGIN,
    ) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin is not None:
            hdrs["Origin"] = origin
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)

    def get(self, path: str, **kw: Any) -> Response:
        return self.request("GET", path, origin=kw.pop("origin", None), **kw)

    def post(self, path: str, payload: Any = None, **kw: Any) -> Response:
        body = json.dumps({} if payload is None else payload).encode()
        headers = {"Content-Type": "application/json", **kw.pop("headers", {})}
        return self.request("POST", path, body, headers, **kw)

    def upload(self, filename: str, content: bytes, **kw: Any) -> Response:
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Imx-Filename": quote(filename, safe=""),
            **kw.pop("headers", {}),
        }
        return self.request("POST", "/resumes", content, headers, **kw)


@dataclass
class Harness:
    client: Client
    service: PresentationService
    candidates: FakeCandidates
    site: FictionalSite
    paths: LocalPaths
    runs: list[ServiceInteraction]

    def wait_idle(self, timeout: float = 10.0) -> None:
        run = self.service.dispatcher.current
        if run is not None:
            assert run.done.wait(timeout), "run did not finish"

    def store(self) -> ApplicationStore:
        return ApplicationStore.open(self.paths.state_db)

    def setup_candidate(self) -> str:
        resume = self.client.upload("Avery Example.pdf", b"%PDF-1.4 fictional resume").json
        return str(resume["id"])

    def profile(self, **overrides: str) -> dict[str, str]:
        base = {
            "firstName": "Avery", "lastName": "Example", "email": "avery@example.test",
            "phone": "+1 555 0100", "location": "Springfield, Oregon, USA",
            "linkedinUrl": "", "websiteUrl": "",
        }
        base.update(overrides)
        return base

    def start(self, url: str = SITE_URL, resume_id: str | None = None) -> Response:
        rid = resume_id or self.setup_candidate()
        return self.client.post(
            "/applications",
            {"applicationUrl": url, "profile": self.profile(), "resumeId": rid},
        )


@pytest.fixture
def harness(
    isolated_imx_home: LocalPaths, mock_form: ApplicationForm, tmp_path: Path
) -> Iterator[Harness]:
    paths = isolated_imx_home
    paths.ensure()
    keep = {"gender", "why_us", "privacy", "heard_from"}
    fields = [
        f.model_copy(update={"required": True}) if f.id == "heard_from" else f
        for f in mock_form.fields
        if f.id in keep
    ]
    site = FictionalSite(form=mock_form.model_copy(update={"url": SITE_URL, "fields": fields}))
    runs: list[ServiceInteraction] = []

    def factory(interaction: ServiceInteraction) -> ScriptedRunner:
        runs.append(interaction)
        return ScriptedRunner(paths, site, interaction)

    config = ServiceConfig(paths=paths, allowed_origin=ORIGIN, port=0, reconcile_wait_s=5.0)
    candidates = FakeCandidates(tmp_path / "private-profile")
    dispatcher = Dispatcher(factory)
    service = PresentationService(config, candidates=candidates, dispatcher=dispatcher)
    service.recover()
    server = make_server(service)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield Harness(
            client=Client(server.server_address[1]), service=service, candidates=candidates,
            site=site, paths=paths, runs=runs,
        )
    finally:
        site.hold.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        dispatcher.shutdown()
