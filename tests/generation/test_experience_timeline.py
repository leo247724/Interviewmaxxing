"""Years of experience derived from the resume timeline: merged overlaps, whole years
rounded down, per-area years from the roles that name the area, nothing above the
timeline, nothing under a year. Fictional profiles only."""
from __future__ import annotations

from datetime import UTC, date, datetime

from interviewmaxxing_core import CandidateProfile, Experience
from interviewmaxxing_generation.knowledge import timeline as tl
from interviewmaxxing_generation.knowledge.stories import StoryRoleLink
from interviewmaxxing_generation.questions import years_fact_area

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TODAY = date(2026, 9, 25)


def with_roles(candidate: CandidateProfile, roles: list[tuple]) -> CandidateProfile:
    base = candidate.verified_facts()[0]
    facts = [base]
    experience = []
    for role_id, company, title, start, end, current, bullet in roles:
        fact_id = f"fact_{role_id}"
        facts.append(base.model_copy(update={"id": fact_id, "key": "experience", "value": bullet,
                                             "evidence": [bullet]}))
        experience.append(Experience(id=role_id, company=company, title=title, start=start, end=end,
                                     current=current, fact_ids=[fact_id]))
    return candidate.model_copy(update={"facts": facts, "experience": experience, "education": []})


def test_merged_months_counts_overlaps_once() -> None:
    assert tl.merged_months([]) == 0
    assert tl.merged_months([(0, 11)]) == 12
    assert tl.merged_months([(0, 11), (6, 23)]) == 24
    assert tl.merged_months([(0, 5), (7, 12)]) == 12
    assert tl.merged_months([(0, 5), (6, 11)]) == 12
    assert tl.merged_months([(10, 20), (0, 30)]) == 31


def test_total_and_per_area_years_round_down_and_never_exceed_the_timeline(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False,
         "Ran paid search campaigns in Google Ads."),
        ("r2", "Crumb & Co.", "Marketing Manager", "2022-07", "2024-06", False,
         "Managed paid media and SEO for twelve stores."),
        ("r3", "Ovenboard Labs", "Founder", "2024-10", None, True,
         "Built a reporting tool in TypeScript."),
    ])
    facts = tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)
    by_key = {fact.key: fact for fact in facts}
    total = by_key["years_experience"]
    assert total.value == 5 and total.id == "derived_years_experience"
    assert total.source == "derived:experience_timeline" and total.is_verified
    assert total.verification.method is not None and total.verification.method.value == "USER_CONFIRMED"
    assert "66 months" in total.evidence[0] and "Glaze Agency (2021-01 to 2022-12)" in total.evidence[1]
    assert by_key["years_experience.paid_search"].value == 2
    assert by_key["years_experience.google_ads"].value == 2
    assert by_key["years_experience.ppc"].value == 2  # from the title
    assert by_key["years_experience.paid_media"].value == 3  # r1 (Google Ads) and r2 (paid media) merged
    assert by_key["years_experience.seo"].value == 2
    assert by_key["years_experience.typescript"].value == 2  # the current role runs to today
    assert all(isinstance(fact.value, int) and fact.value <= total.value for fact in facts)
    assert all(fact.id == "derived_" + fact.key.replace(".", "_") for fact in facts)
    assert years_fact_area("years_experience.paid_media") == "paid media" and years_fact_area("years_experience") == ""
    assert [fact.id for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)] == [fact.id for fact in facts]


def test_under_a_year_yields_no_fact_and_undated_roles_are_skipped(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran paid search campaigns."),
        ("r2", "Pop-up Bakery", "Consultant", "2023-01", "2023-08", False, "Built HubSpot automations."),
        ("r3", "Nowhere", "Intern", None, None, False, "Used Meta Ads."),
    ])
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert facts["years_experience"] == 2  # 24 + 8 months, rounded down
    assert "years_experience.hubspot" not in facts and "years_experience.meta_ads" not in facts
    assert facts["years_experience.paid_search"] == 2
    short = with_roles(fictional_candidate, [("r1", "Pop-up", "Intern", "2026-01", "2026-06", False, "Ran ads.")])
    assert tl.derive_experience_years(short, today=TODAY, verified_at=NOW) == []
    none = fictional_candidate.model_copy(update={"experience": [], "education": []})
    assert tl.derive_experience_years(none, today=TODAY, verified_at=NOW) == []


def test_linked_stories_add_their_tools_to_the_linked_role(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran campaigns."),
        ("r2", "Crumb & Co.", "Baker", "2023-01", "2023-12", False, "Baked bread."),
    ])
    link = StoryRoleLink("story1", "r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False,
                         "jev_match", 0.95, 0.98)
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(
        profile, today=TODAY, verified_at=NOW, story_links={"story1": link},
        story_areas={"story1": ["Google Ads", "call tracking"], "unlinked": ["Meta Ads"]})}
    assert facts["years_experience.google_ads"] == 2 and facts["years_experience.call_tracking"] == 2
    assert "years_experience.meta_ads" not in facts
    assert facts["years_experience"] == 3


def test_a_role_naming_a_member_counts_toward_its_families(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran Google Ads campaigns."),
        ("r2", "Crumb & Co.", "Social Lead", "2023-01", "2024-12", False, "Ran Meta Ads for the stores."),
        ("r3", "Quill Press", "SEO Editor", "2025-01", "2025-12", False, "Wrote SEO briefs."),
    ])
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert facts["years_experience.paid_search"] == 2 and facts["years_experience.paid_social"] == 2
    assert facts["years_experience.paid_media"] == 4  # both ads roles, not the SEO one
    assert facts["years_experience.digital_marketing"] == 5 and facts["years_experience"] == 5
    assert "years_experience.programmatic_advertising" not in facts  # never implied
    assert tl.with_families({"SEO"}) == {"SEO", "digital marketing"}
    assert tl.with_families({"Google Ads"}) == {"Google Ads", "paid search", "paid media", "digital marketing"}
    assert tl.with_families(set()) == set()


def test_story_facts_in_the_profile_add_areas_to_their_linked_role(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran campaigns."),
    ])
    story_fact = profile.facts[0].model_copy(update={
        "id": "sf_story1_abc", "key": "skills", "source": "story:" + "0" * 64,
        "value": "I built the reporting in Looker Studio and ran call tracking (resume: Glaze Agency, 2021-01 to 2022-12)",
        "evidence": ["Story 01: x", "period_source: resume_role", "story_source: candidate-stories", "resume_role_id: r1"]})
    profile = profile.model_copy(update={"facts": [*profile.facts, story_fact]})
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert facts["years_experience.looker_studio"] == 2 and facts["years_experience.call_tracking"] == 2
