"""Knowledge imports preserve the canonical profile and explicit verification."""
import json

import pytest

from interviewmaxxing_core import CandidateFact, FactVerification, VerificationStatus


def test_merge_and_revoke_do_not_upgrade_or_replace_unrelated_data(write_candidate, candidate_store):
    directory = write_candidate()
    before = json.loads((directory / "profile.json").read_text())
    profile = candidate_store.load("default")
    existing = profile.facts[0]
    revoked = existing.model_copy(update={
        "verification": FactVerification(status=VerificationStatus.UNVERIFIED),
    })
    imported = CandidateFact(id="additional", key="experience", value="An unconfirmed statement",
        source="unconfirmed text", verification=FactVerification(status=VerificationStatus.UNVERIFIED))
    updated = candidate_store.upsert_facts("default", [revoked, imported])
    assert not updated.find_fact(existing.id).is_verified
    assert not updated.find_fact("additional").is_verified
    after = json.loads((directory / "profile.json").read_text())
    for key in before.keys() - {"facts"}:
        assert after[key] == before[key]
    assert len(updated.facts) == len(profile.facts) + 1
    stable = (directory / "profile.json").read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        candidate_store.upsert_facts("default", [imported, imported])
    assert (directory / "profile.json").read_bytes() == stable
