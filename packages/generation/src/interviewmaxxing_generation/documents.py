"""Offline, fact-preserving document drafts; independent of packet resolution.

Only explicitly document-ready, verified, standalone fact text is rendered. A
writing provider may propose claims, but a citation is not proof of a paraphrase:
this prototype accepts verbatim claims only. It never selects an upload artifact.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import Literal, Protocol

from interviewmaxxing_core import CandidateFact, CandidateProfile, JobRecord, ResumeArtifact

DOCUMENT_VERSION = "offline-documents-v1"
SECTION_KEYS = {
    "resume.summary": "Professional summary",
    "resume.experience": "Experience",
    "resume.achievement": "Experience",
    "resume.skill": "Skills",
    "resume.education": "Education and credentials",
    "resume.credential": "Education and credentials",
}
SECTIONS = tuple(dict.fromkeys(SECTION_KEYS.values()))


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _visible(value: str) -> bool:
    # Do not place hidden instructions, multi-line blocks or markup in documents.
    return bool(value.strip()) and all(
        not unicodedata.category(c).startswith("C") and unicodedata.category(c) not in {"Zl", "Zp"}
        for c in value
    )


def _markdown(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>()#!|])", r"\\\1", value)


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold())) - {
        "a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to", "with",
    }


@dataclass(frozen=True)
class JobDocumentEvidence:
    """A source snapshot, not instructions. Priorities affect ordering only.

    ``priority_terms`` are explicit retrieval terms from the caller/Jev triage;
    they are not qualification decisions. Raw description is hashed for version
    binding but is never sent to the writing provider or copied into documents.
    """

    job_id: str
    source_ref: str
    source_version: str
    description: str
    priority_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentScope:
    candidate_id: str
    job_id: str
    candidate_version: str
    job_version: str
    evidence_version: str
    original_sha256: str


@dataclass(frozen=True)
class DocumentIssue:
    code: str
    reference: str
    detail: str


@dataclass(frozen=True)
class FactCitation:
    fact_id: str
    source: str
    evidence: tuple[str, ...]
    verification_method: str
    verified_at: str
    fact_sha256: str


@dataclass(frozen=True)
class SupportedClaim:
    fact_id: str
    text: str
    section: str
    citation: FactCitation


@dataclass(frozen=True)
class ResumeVariantPlan:
    scope: DocumentScope
    ordered_claims: tuple[SupportedClaim, ...]
    changes: tuple[str, ...]
    issues: tuple[DocumentIssue, ...]


@dataclass(frozen=True)
class WritingBrief:
    """Immutable, scoped inputs without original paths or raw job instructions."""

    scope: DocumentScope
    company: str
    title: str
    claims: tuple[SupportedClaim, ...]
    max_claims: int = 2


@dataclass(frozen=True)
class ProposedClaim:
    fact_id: str
    text: str


@dataclass(frozen=True)
class WritingProposal:
    scope: DocumentScope
    claims: tuple[ProposedClaim, ...]


class WritingProvider(Protocol):
    """Injectable claim-writing seam; no live provider ships in this prototype.

    Future free prose needs independent entailment/review, not just citation IDs.
    Exceptions propagate; there is no silent fallback claiming provider success.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    def draft(self, brief: WritingBrief) -> WritingProposal: ...


@dataclass(frozen=True)
class DocumentArtifact:
    kind: Literal["resume", "cover_letter"]
    content: str
    sha256: str
    source_fact_ids: tuple[str, ...]
    media_type: str = "text/markdown; charset=utf-8"

    def verify(self) -> bool:
        return hashlib.sha256(self.content.encode()).hexdigest() == self.sha256


@dataclass(frozen=True)
class DocumentBundle:
    schema_version: str
    scope: DocumentScope
    original: ResumeArtifact
    job_evidence: JobDocumentEvidence
    plan: ResumeVariantPlan
    resume: DocumentArtifact
    cover_letter: DocumentArtifact | None
    provider_id: str
    provider_version: str
    issues: tuple[DocumentIssue, ...]
    bundle_sha256: str
    review_required: bool = True

    def manifest(self) -> dict[str, object]:
        """JSON-ready sidecar, including citations and versions, outside prose."""
        return {
            "schema_version": self.schema_version,
            "scope": asdict(self.scope),
            "original": self.original.model_dump(mode="json"),
            "job_evidence": asdict(self.job_evidence),
            "plan": asdict(self.plan),
            "resume": asdict(self.resume),
            "cover_letter": asdict(self.cover_letter) if self.cover_letter else None,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "issues": [asdict(issue) for issue in self.issues],
            "review_required": self.review_required,
        }

    def to_json(self) -> str:
        return json.dumps({**self.manifest(), "bundle_sha256": self.bundle_sha256},
                          ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def assert_current(self, candidate: CandidateProfile, job: JobRecord,
                       evidence: JobDocumentEvidence) -> None:
        """Reject stale/cross-candidate/cross-job reuse before any later selection."""
        if self.scope != document_scope(candidate, job, evidence):
            raise ValueError("document bundle belongs to a different candidate/job/evidence version")
        if not self.resume.verify() or (self.cover_letter and not self.cover_letter.verify()):
            raise ValueError("document artifact digest mismatch")
        if _digest(self.manifest()) != self.bundle_sha256:
            raise ValueError("document bundle digest mismatch")


def document_scope(candidate: CandidateProfile, job: JobRecord,
                   evidence: JobDocumentEvidence) -> DocumentScope:
    if evidence.job_id != job.id:
        raise ValueError("job evidence belongs to another job")
    if not evidence.source_ref.strip() or not evidence.source_version.strip():
        raise ValueError("job evidence requires source reference and version")
    if job.merged_into:
        raise ValueError("use the canonical job, not a merged job record")
    if not candidate.resume.verify():
        raise ValueError("original resume is missing or its digest changed")
    return DocumentScope(
        candidate_id=candidate.id, job_id=job.id,
        candidate_version=_digest(candidate.model_dump(mode="json")),
        job_version=_digest(job.model_dump(mode="json")),
        evidence_version=_digest({"job_evidence": asdict(evidence),
                                 "candidate_facts": [f.model_dump(mode="json")
                                                     for f in candidate.facts]}),
        original_sha256=candidate.resume.sha256,
    )


def _claim(fact: CandidateFact) -> SupportedClaim:
    assert isinstance(fact.value, str)
    assert fact.verification.method is not None and fact.verification.verified_at is not None
    return SupportedClaim(fact.id, fact.value, SECTION_KEYS[fact.key], FactCitation(
        fact_id=fact.id, source=fact.source, evidence=tuple(fact.evidence),
        verification_method=fact.verification.method.value,
        verified_at=fact.verification.verified_at.isoformat(),
        fact_sha256=_digest(fact.model_dump(mode="json")),
    ))


def plan_resume_variant(candidate: CandidateProfile, job: JobRecord,
                        evidence: JobDocumentEvidence) -> ResumeVariantPlan:
    scope = document_scope(candidate, job, evidence)
    claims: list[SupportedClaim] = []
    issues: list[DocumentIssue] = []
    for fact in candidate.facts:
        if not fact.key.startswith("resume."):
            continue  # Never promote salary, consent, identity or other answers to prose.
        if fact.key not in SECTION_KEYS:
            issues.append(DocumentIssue("UNSUPPORTED_FACT_KEY", fact.id,
                                        "Use an explicitly supported document-ready fact key."))
        elif not fact.is_verified:
            issues.append(DocumentIssue("UNVERIFIED_FACT", fact.id,
                                        "Candidate confirmation is missing; claim omitted."))
        elif not isinstance(fact.value, str) or not _visible(fact.value):
            issues.append(DocumentIssue("UNSUPPORTED_FACT_TEXT", fact.id,
                                        "A visible single-line standalone statement is required."))
        else:
            claims.append(_claim(fact))

    # Retrieval relevance is not evidence that the candidate meets a requirement.
    terms = [_tokens(term) for term in evidence.priority_terms]
    for term, tokens in zip(evidence.priority_terms, terms, strict=True):
        if not tokens or not any(tokens <= _tokens(c.text) for c in claims):
            issues.append(DocumentIssue("MISSING_TERM_EVIDENCE", term,
                                        "No document-ready verified claim contains these terms; "
                                        "this is not a qualification assessment."))
    ranked = sorted(claims, key=lambda c: -sum(len(t & _tokens(c.text)) for t in terms))
    ordered = tuple(c for section in SECTIONS for c in ranked if c.section == section)
    changes = ["Rendered verified document-ready statements under semantic single-column headings.",
               "Kept every included claim verbatim; original resume bytes were not edited."]
    if [c.fact_id for c in ordered] != [c.fact_id for c in claims]:
        changes.append("Grouped claims by semantic section and ranked within sections using job priority term overlap.")
    if not claims:
        issues.append(DocumentIssue("MISSING_DOCUMENT_FACTS", candidate.id,
                                    "No supported verified document-ready statements are available."))
    return ResumeVariantPlan(scope, ordered, tuple(changes), tuple(issues))


def _artifact(kind: Literal["resume", "cover_letter"], content: str,
              claims: tuple[SupportedClaim, ...]) -> DocumentArtifact:
    return DocumentArtifact(kind, content, hashlib.sha256(content.encode()).hexdigest(),
                            tuple(c.fact_id for c in claims))


def _render_resume(candidate: CandidateProfile, plan: ResumeVariantPlan) -> DocumentArtifact:
    # Identity is explicitly verified by the core CandidateIdentity contract.
    if not _visible(candidate.identity.full_name):
        raise ValueError("candidate name must be visible single-line text")
    lines = [f"# {_markdown(candidate.identity.full_name)}", ""]
    for section in SECTIONS:
        claims = [c for c in plan.ordered_claims if c.section == section]
        if claims:
            lines.extend([f"## {section}", ""])
            lines.extend(f"- {_markdown(c.text)}" for c in claims)
            lines.append("")
    return _artifact("resume", "\n".join(lines), plan.ordered_claims)


def build_document_bundle(candidate: CandidateProfile, job: JobRecord,
                          evidence: JobDocumentEvidence, *,
                          provider: WritingProvider | None = None) -> DocumentBundle:
    """Prepare reviewable Markdown in memory. No writes, pinning or submission.

    This is a separate next-stage API; it does not implement PacketResolver and
    cannot replace the runner's selected resume pin. Deterministic templates are explicitly
    identified as templates, never as Jev or a live writing model.
    """
    plan = plan_resume_variant(candidate, job, evidence)
    issues = list(plan.issues)
    eligible = tuple(c for c in plan.ordered_claims if c.section == "Experience")
    provider_id, provider_version = "deterministic-template", DOCUMENT_VERSION
    accepted: tuple[SupportedClaim, ...] = eligible[:2]
    valid_target = bool(job.company and job.title and _visible(job.company) and _visible(job.title))
    if not valid_target:
        issues.append(DocumentIssue("MISSING_JOB_TARGET", job.id,
                                    "A visible company and title are required for a cover letter."))
        accepted = ()
    elif not eligible:
        issues.append(DocumentIssue("MISSING_COVER_EVIDENCE", candidate.id,
                                    "A verified experience/accomplishment statement is required."))
    elif provider is not None:
        assert job.company and job.title
        provider_id, provider_version = provider.provider_id, provider.provider_version
        if not _visible(provider_id) or not _visible(provider_version):
            raise ValueError("writing provider identity and version are required")
        proposal = provider.draft(WritingBrief(plan.scope, job.company, job.title, eligible))
        accepted = ()
        if proposal.scope != plan.scope:
            issues.append(DocumentIssue("PROVIDER_SCOPE_MISMATCH", provider_id,
                                        "Proposal belongs to different candidate/job/evidence versions."))
        else:
            by_id = {c.fact_id: c for c in eligible}
            accepted_list: list[SupportedClaim] = []
            seen: set[str] = set()
            for proposed in proposal.claims:
                source = by_id.get(proposed.fact_id)
                if source is None or proposed.text != source.text:
                    issues.append(DocumentIssue("UNSUPPORTED_CLAIM", proposed.fact_id,
                                                "Only verbatim eligible verified claims are accepted; "
                                                "rewrites require independent review."))
                elif proposed.fact_id in seen or len(accepted_list) >= 2:
                    issues.append(DocumentIssue("EXCESS_CLAIM", proposed.fact_id,
                                                "Cover letters accept at most two distinct claims."))
                else:
                    seen.add(proposed.fact_id)
                    accepted_list.append(source)
            accepted = tuple(accepted_list)
            if not accepted:
                issues.append(DocumentIssue("MISSING_COVER_EVIDENCE", provider_id,
                                            "No supported provider claims were accepted."))

    cover = None
    if accepted:
        assert job.title and job.company
        text = (f"I am applying for the {_markdown(job.title)} role at {_markdown(job.company)}.\n\n"
                + "\n\n".join(_markdown(c.text) for c in accepted)
                + "\n\nThank you for considering my application.\n")
        # Very long verified statements are not silently truncated or paraphrased.
        if len(text.split()) > 180:
            issues.append(DocumentIssue("COVER_TOO_LONG", candidate.id,
                                        "Cover exceeds 180 words; provide concise verified statements."))
        else:
            cover = _artifact("cover_letter", text, accepted)
    if document_scope(candidate, job, evidence) != plan.scope:
        raise ValueError("candidate/job/evidence changed during document preparation")
    bundle = DocumentBundle(
        DOCUMENT_VERSION, plan.scope, candidate.resume.model_copy(deep=True), evidence, plan,
        _render_resume(candidate, plan), cover, provider_id, provider_version, tuple(issues), "",
    )
    return replace(bundle, bundle_sha256=_digest(bundle.manifest()))
