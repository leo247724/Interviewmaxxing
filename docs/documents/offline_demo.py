"""Write a new fictional document bundle directory; no network or real profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from interviewmaxxing_core import CandidateProfile, JobRecord, ResumeArtifact
from interviewmaxxing_generation import JobDocumentEvidence, build_document_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="New directory (must not already exist)")
    output = parser.parse_args().output_dir.resolve()
    root = Path(__file__).resolve().parents[2]
    profile = json.loads((root / "tests/fixtures/core/candidate_profile.json").read_text())
    facts = json.loads((root / "tests/fixtures/generation/document_facts.json").read_text())
    for index, fact in enumerate(facts, start=1):
        fact.update(source="fictional original and fictional user confirmation",
                    evidence=[f"original.txt:line-{index}"],
                    verification={"status": "VERIFIED", "method": "USER_CONFIRMED",
                                  "verified_at": "2026-09-22T20:00:00Z"})
    output.mkdir()  # Exclusive directory creation: never overwrite an earlier bundle.
    original = output / "original.txt"
    original.write_text("\n".join(f["value"] for f in facts) + "\n", encoding="utf-8")
    profile.update(facts=facts, experience=[], education=[], saved_answers=[],
                   resume=ResumeArtifact.from_file(original, id="fictional-original",
                                                   media_type="text/plain").model_dump(mode="json"))
    candidate = CandidateProfile.model_validate(profile)
    job = JobRecord(id="fictional-paid-search-job", company="Lantern Fictional Labs",
                    title="Paid Search Manager", application_url="https://example.invalid/job",
                    normalized_url="https://example.invalid/job",
                    created_at="2026-09-22T20:00:00Z", updated_at="2026-09-22T20:00:00Z")
    evidence = JobDocumentEvidence(job.id, job.application_url, "fictional-v1",
                                   "Manage paid search and landing page experimentation.",
                                   ("paid search", "landing page"))
    bundle = build_document_bundle(candidate, job, evidence)
    (output / "resume.md").write_text(bundle.resume.content, encoding="utf-8")
    if bundle.cover_letter:
        (output / "cover-letter.md").write_text(bundle.cover_letter.content, encoding="utf-8")
    (output / "document-bundle.json").write_text(bundle.to_json(), encoding="utf-8")
    bundle.assert_current(candidate, job, evidence)
    assert (output / "resume.md").read_text(encoding="utf-8") == bundle.resume.content
    if bundle.cover_letter:
        assert (output / "cover-letter.md").read_text(encoding="utf-8") == bundle.cover_letter.content
    print(f"Fictional documents written and verified: {output}")
    print(f"Bundle digest: {bundle.bundle_sha256}")


if __name__ == "__main__":
    main()
