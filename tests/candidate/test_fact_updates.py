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


def test_remove_facts_deletes_by_id_and_drops_group_references(write_candidate, candidate_store):
    directory = write_candidate()
    profile = candidate_store.load("default")
    target = profile.facts[0]
    before_groups = [group.id for group in profile.experience]
    updated = candidate_store.remove_facts("default", [target.id, "unknown-fact-id"])
    assert updated.find_fact(target.id) is None
    assert len(updated.facts) == len(profile.facts) - 1
    assert all(target.id not in group.fact_ids for group in [*updated.experience, *updated.education])
    assert [group.id for group in updated.experience] == before_groups
    after = json.loads((directory / "profile.json").read_text())
    assert target.id not in json.dumps(after["facts"])
    assert after["identity"] == profile.identity.model_dump(mode="json")
    with pytest.raises(TypeError):
        candidate_store.remove_facts("default", [""])
    assert candidate_store.load("default").find_fact(target.id) is None
