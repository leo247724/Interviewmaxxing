"""Years of experience derived from the resume timeline: merged overlaps, whole years
rounded down, per-area years from the titles that name an area (the whole role) and the
bullets that state their own duration, nothing above the timeline, nothing under a year;
the total verified, each area unverified until the person confirms it. Fictional
profiles only."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from interviewmaxxing_core import CandidateProfile, Experience
from interviewmaxxing_generation.knowledge import timeline as tl
from interviewmaxxing_generation.questions import years_fact_area

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
TODAY = date(2026, 9, 25)


def with_roles(candidate: CandidateProfile, roles: list[tuple]) -> CandidateProfile:
    base = candidate.verified_facts()[0]
    facts = [base]
    experience = []
    for role_id, company, title, start, end, current, bullets in roles:
        texts = [bullets] if isinstance(bullets, str) else list(bullets)
        fact_ids = []
        for index, bullet in enumerate(texts):
            fact_id = f"fact_{role_id}_{index}"
            facts.append(base.model_copy(update={"id": fact_id, "key": "experience", "value": bullet,
                                                 "evidence": [bullet]}))
            fact_ids.append(fact_id)
        experience.append(Experience(id=role_id, company=company, title=title, start=start, end=end,
                                     current=current, fact_ids=fact_ids))
    return candidate.model_copy(update={"facts": facts, "experience": experience, "education": []})


def test_merged_months_counts_overlaps_once() -> None:
    assert tl.merged_months([]) == 0
    assert tl.merged_months([(0, 11)]) == 12
    assert tl.merged_months([(0, 11), (6, 23)]) == 24
    assert tl.merged_months([(0, 5), (7, 12)]) == 12
    assert tl.merged_months([(0, 5), (6, 11)]) == 12
    assert tl.merged_months([(10, 20), (0, 30)]) == 31


def test_the_total_is_verified_and_a_title_dates_its_areas_for_the_whole_role(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False,
         "Ran paid search campaigns in Google Ads."),  # a mention: dates nothing by itself
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
    # The title "PPC Specialist" dates PPC and its families for the whole role; the bullets
    # name Google Ads, paid media, SEO and TypeScript without a duration: no area fact.
    assert set(by_key) == {"years_experience", "years_experience.ppc", "years_experience.paid_search",
                           "years_experience.paid_media", "years_experience.digital_marketing"}
    assert by_key["years_experience.ppc"].value == 2 and by_key["years_experience.paid_media"].value == 2
    for fact in facts[1:]:
        assert not fact.is_verified and fact.verification.method is None and fact.verification.verified_at is None
        assert fact.evidence[1] == "basis: Glaze Agency: title, 24 months" and fact.evidence[2] == tl.CONFIRMATION_NOTE
        assert fact.id == "derived_" + fact.key.replace(".", "_") and fact.value <= total.value
    assert years_fact_area("years_experience.paid_media") == "paid media" and years_fact_area("years_experience") == ""
    assert [f.id for f in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)] == [f.id for f in facts]


def test_a_bullet_dates_an_area_only_by_the_duration_it_states(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Crumb & Co.", "Marketing Manager", "2020-01", "2023-12", False, [
            "Piloted TikTok Ads in Q4.",                         # a one-time mention: no fact
            "Ran paid social on Meta Ads for 18 months.",        # its own duration
            "Managed Google Ads for over 3 years.",
            "Owned SEO from 2021 to 2023.",                      # a year range: the years between
            "Ran email marketing for 10 years.",                 # longer than the role: capped
        ]),
    ])
    facts = {fact.key: fact for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert facts["years_experience"].value == 4
    assert "years_experience.tiktok_ads" not in facts
    assert facts["years_experience.meta_ads"].value == 1 and facts["years_experience.paid_social"].value == 1
    assert facts["years_experience.google_ads"].value == 3 and facts["years_experience.paid_search"].value == 3
    assert facts["years_experience.seo"].value == 2
    assert facts["years_experience.email_marketing"].value == 4
    assert facts["years_experience.paid_media"].value == 3  # the longest dated bullet of the family
    assert facts["years_experience.digital_marketing"].value == 4
    assert facts["years_experience.google_ads"].evidence[1] == "basis: Crumb & Co.: a bullet stating 36 months"
    assert all(not fact.is_verified for key, fact in facts.items() if key != "years_experience")


@pytest.mark.parametrize("text,months", [
    ("Ran paid social on Meta Ads for 18 months.", 18),
    ("Managed Google Ads for over 3 years.", 36),
    ("About a year of SEO work.", 12),
    ("Two years of PPC.", 24),
    ("2.5 years of email marketing.", 30),
    ("Under a year of PPC.", None),
    ("Piloted TikTok Ads in Q4.", None),
    ("Owned SEO from 2021 to 2023.", 24),
    ("Ran paid search 2019-present.", 84),
    ("Ran paid search 2023 to 2021.", None),  # not a range
])
def test_stated_duration_months(text: str, months: int | None) -> None:
    assert tl.stated_duration_months(text, today=TODAY) == months


def test_under_a_year_yields_no_fact_and_undated_roles_are_skipped(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran paid search campaigns."),
        ("r2", "Pop-up Bakery", "Consultant", "2023-01", "2023-08", False, "Built HubSpot automations for 8 months."),
        ("r3", "Nowhere", "Intern", None, None, False, "Used Meta Ads for 5 years."),
    ])
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert facts["years_experience"] == 2  # 24 + 8 months, rounded down
    assert "years_experience.hubspot" not in facts and "years_experience.meta_ads" not in facts
    assert facts["years_experience.paid_search"] == 2 and facts["years_experience.ppc"] == 2
    short = with_roles(fictional_candidate, [("r1", "Pop-up", "Intern", "2026-01", "2026-06", False, "Ran ads.")])
    assert tl.derive_experience_years(short, today=TODAY, verified_at=NOW) == []
    none = fictional_candidate.model_copy(update={"experience": [], "education": []})
    assert tl.derive_experience_years(none, today=TODAY, verified_at=NOW) == []


def test_a_role_naming_a_member_counts_toward_its_families(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran Google Ads campaigns."),
        ("r2", "Crumb & Co.", "Social Lead", "2023-01", "2024-12", False, "Ran Meta Ads for the stores for 2 years."),
        ("r3", "Quill Press", "SEO Editor", "2025-01", "2025-12", False, "Wrote SEO briefs."),
    ])
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert facts["years_experience.paid_search"] == 2 and facts["years_experience.paid_social"] == 2
    assert facts["years_experience.paid_media"] == 4  # the PPC title and the dated Meta Ads bullet
    assert facts["years_experience.digital_marketing"] == 5 and facts["years_experience"] == 5
    assert facts["years_experience.seo"] == 1  # from the title
    assert "years_experience.programmatic_advertising" not in facts  # never implied
    assert tl.with_families({"SEO"}) == {"SEO", "digital marketing"}
    assert tl.with_families({"Google Ads"}) == {"Google Ads", "paid search", "paid media", "digital marketing"}
    assert tl.with_families(set()) == set()


def test_stories_never_date_an_area(fictional_candidate: CandidateProfile) -> None:
    profile = with_roles(fictional_candidate, [
        ("r1", "Glaze Agency", "PPC Specialist", "2021-01", "2022-12", False, "Ran campaigns."),
    ])
    story_fact = profile.facts[0].model_copy(update={
        "id": "sf_story1_abc", "key": "skills", "source": "story:" + "0" * 64,
        "value": "I built the reporting in Looker Studio and ran call tracking (resume: Glaze Agency, 2021-01 to 2022-12)",
        "evidence": ["Story 01: x", "period_source: resume_role", "story_source: candidate-stories", "resume_role_id: r1"]})
    profile = profile.model_copy(update={"facts": [*profile.facts, story_fact]})
    facts = {fact.key: fact.value for fact in tl.derive_experience_years(profile, today=TODAY, verified_at=NOW)}
    assert "years_experience.looker_studio" not in facts and "years_experience.call_tracking" not in facts
    assert facts["years_experience.ppc"] == 2
    [span] = tl.role_spans(profile, today=TODAY)
    assert span.areas == span.title_areas and span.bullet_areas == {}
