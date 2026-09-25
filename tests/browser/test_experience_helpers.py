"""Round 11: the deterministic experience helpers (``ai/experience.py``). Fictional facts only."""
from __future__ import annotations

from typing import Any

import pytest

from interviewmaxxing_browser.ai.experience import (
    PLATFORMS,
    YearsRequirement,
    area_facts,
    experience_wording,
    platform_facts,
    platforms_named,
    prefer_stated,
    total_facts,
    years_requirement,
    years_value,
)
from interviewmaxxing_core import CandidateFact

STATED, DERIVED, RESUME = "user:years", "derived:experience_timeline", "resume"
STORY, USER_STORY = "story:" + "d" * 64, "user:story"


def fact(key: str, value: Any, *, source: str = STATED, fid: str | None = None) -> CandidateFact:
    return CandidateFact(id=fid or f"{source.split(':')[0]}.{key}", key=key, value=value, source=source,
                         verification={"status": "VERIFIED", "method": "USER_STATED",
                                       "verified_at": "2026-09-01T12:00:00Z"})


def ids(facts: list[CandidateFact]) -> list[str]:
    return [f.id for f in facts]


# --- years_value and total_facts --------------------------------------------------------------

@pytest.mark.parametrize("value,years", [
    (7, 7.0), (7.5, 7.5), (0, 0.0), ("7", 7.0), ("7 years", 7.0), (" 1 year ", 1.0),
    ("7.5 yrs", 7.5), ("7+", 7.0), ("8 YEARS", 8.0),
    ("about 7", None), ("7-8 years", None), ("seven", None), ("7 years at Fictional Co", None),
    (True, None), (False, None), (None, None), (["7"], None),
])
def test_years_value_reads_one_stated_number_of_years(value: Any, years: float | None) -> None:
    assert years_value(fact("years_experience", value)) == years


def test_total_facts_are_the_generic_total_keys_stating_a_number() -> None:
    facts = [fact("years_experience", 8, fid="total"), fact("years_experience.total", "8 years", fid="dotted"),
             fact("years_professional_experience", 8, fid="professional"),
             fact("years_experience.paid_media", 7, fid="area"), fact("years_experience", "lots", fid="words"),
             fact("skills", "Fictional analytics", fid="skill")]
    assert ids(total_facts(facts)) == ["total", "dotted", "professional"]


# --- prefer_stated ----------------------------------------------------------------------------

@pytest.mark.parametrize("stated_key,derived_key", [
    ("years_experience", "years_experience"),
    ("years_experience.total", "years_experience"),
    ("years_professional_experience", "years_experience"),
    ("years_experience", "years_experience.total"),
    ("years_experience", "years_professional_experience"),
    ("years_experience.paid_media", "years_experience.paid_media"),
])
def test_a_stated_fact_replaces_the_derived_fact_for_the_same_key(stated_key: str, derived_key: str) -> None:
    stated = fact(stated_key, 8, source="user:simple-answers", fid="stated")
    derived = fact(derived_key, 5, source=DERIVED, fid="derived")
    other = fact("skills", "Fictional analytics", source=RESUME, fid="skill")
    for facts in ([derived, stated, other], [stated, other, derived]):
        kept = prefer_stated(facts)
        assert "derived" not in ids(kept) and ids(kept) == [f.id for f in facts if f.id != "derived"]
        assert next(f for f in kept if f.id == "stated").value == 8  # never averaged or replaced


def test_a_derived_fact_stays_when_nothing_stated_its_key() -> None:
    facts = [fact("years_experience", 5, source=DERIVED, fid="derived_total"),
             fact("years_experience.seo", 3, source=DERIVED, fid="derived_seo"),
             fact("years_experience.paid_media", 7, fid="stated_paid_media"),
             fact("years_experience.seo", 4, source=RESUME, fid="resume_seo"),
             fact("years_experience.meta_ads", 2, source=STORY, fid="story_meta")]
    # A stated area never replaces the derived total or another area; a resume or story
    # source is not the person's own statement.
    assert ids(prefer_stated(facts)) == ids(facts)
    assert prefer_stated([]) == []


def test_one_stated_total_drops_every_derived_spelling_of_the_total() -> None:
    facts = [fact("years_experience", 5, source=DERIVED, fid="derived_plain"),
             fact("years_experience.total", 5, source=DERIVED, fid="derived_dotted"),
             fact("years_experience.paid_media", 6, source=DERIVED, fid="derived_area"),
             fact("years_professional_experience", 8, source="user:simple-answers", fid="stated")]
    assert ids(prefer_stated(iter(facts))) == ["derived_area", "stated"]


# --- years_requirement ------------------------------------------------------------------------

@pytest.mark.parametrize("question,years,strict,area", [
    ("Do you have at least 8 years of total experience?", 8, False, None),
    ("Do you have at least 9 years of total experience?", 9, False, None),
    ("Do you have at least 8 years of total experience in direct response marketing?", 8, False,
     "direct response marketing"),
    ("Do you have 5+ years of paid media experience?", 5, False, "paid media"),
    ("Do you have more than 7 years of experience managing Google Ads?", 7, True, "google ads"),
    ("Do you have more than 7 years of Google Ads experience?", 7, True, "google ads"),
    ("Do you have over five years of SEO experience?", 5, True, "seo"),
    ("Over 10 yrs of exp in SEO?", 10, True, "seo"),
    ("Do you have a minimum of three years of experience?", 3, False, None),
    ("Do you have at least twelve years of experience?", 12, False, None),
    ("AT LEAST EIGHT YEARS OF TOTAL EXPERIENCE?", 8, False, None),
    ("At least ten years of professional experience?", 10, False, None),
    ("No less than 4 yrs of relevant work experience", 4, False, None),
    ("Do you have more than 2 years of full-time work experience?", 2, True, None),
    ("Do you have at least 5+ years of experience?", 5, False, None),
    ("Do you have 3 or more years of B2B marketing experience?", 3, False, "b2b marketing"),
    ("Min. of 2 years experience with LinkedIn Ads", 2, False, "linkedin ads"),
    ("Do you have 5 or more yrs of hands-on experience with Meta Ads?", 5, False, "meta ads"),
    ("Do you have at least 3 years managing paid social campaigns?", 3, False,
     "managing paid social campaigns"),
])
def test_years_requirement_reads_the_threshold_its_strictness_and_the_area(
    question: str, years: float, strict: bool, area: str | None,
) -> None:
    assert years_requirement(question) == YearsRequirement(years=float(years), strict=strict, area=area)


@pytest.mark.parametrize("question", [
    "How many years of experience do you have?",
    "Do you have experience with Meta Ads?",
    "Do you have SEO AND GEO optimization experience?",
    "Are you available to work 40 hours a week?",
    "Which paid media platforms have you directly managed?",
    "",
])
def test_years_requirement_is_none_without_a_years_threshold(question: str) -> None:
    assert years_requirement(question) is None


@pytest.mark.parametrize("question", [
    "Do you have 5+ years of experience, including 2 in paid social?",
    "Do you have at least 8 years in total, including 3 in direct response?",
    "Do you have 5+ years of experience managing teams of 10 or more?",
])
def test_years_requirement_is_none_for_a_second_minimum_without_its_unit(question: str) -> None:
    """Round 12b: a number of its own beside the threshold sets a second minimum (the
    docstring's own example); Jev reads the question instead."""
    assert years_requirement(question) is None


@pytest.mark.parametrize("question", [
    "Do you have 5+ years of experience managing $1M+ budgets?",
    "Do you have 5+ years of B2B marketing experience?",
    "Do you have 5+ years of experience with GA4?",
    "Do you have 5+ years of experience growing revenue by 50%?",
    "Do you have 5+ years of SEO experience in the past 10 years?",
])
def test_years_requirement_keeps_numbers_that_set_no_second_minimum(question: str) -> None:
    """An amount, a number inside a word and another years mention are not a second minimum."""
    requirement = years_requirement(question)
    assert requirement is not None and requirement.years == 5


@pytest.mark.parametrize("requirement,value,met", [
    (YearsRequirement(8, False, None), 8, True), (YearsRequirement(8, False, None), 7.9, False),
    (YearsRequirement(8, False, None), 9, True), (YearsRequirement(7, True, "google ads"), 7, False),
    (YearsRequirement(7, True, "google ads"), 7.5, True), (YearsRequirement(5, False, "paid media"), 5, True),
    (YearsRequirement(5, True, "seo"), 5, False), (YearsRequirement(5, True, "seo"), 6, True),
])
def test_a_strict_requirement_is_met_only_above_its_years(
    requirement: YearsRequirement, value: float, met: bool,
) -> None:
    assert requirement.met_by(value) is met


# --- area_facts -------------------------------------------------------------------------------

AREA_FACTS = [
    fact("years_experience", 8, fid="total"),
    fact("years_experience.paid_media", 7, fid="paid_media"),
    fact("years_experience.meta_ads", 7, fid="meta_ads"),
    fact("years_experience.google_ads", 7, fid="google_ads"),
    fact("years_experience.paid_search", 3, fid="paid_search"),
    fact("years_experience.linkedin_ads", 7, fid="linkedin_ads"),
    fact("years_experience.paid_social", 2, source=DERIVED, fid="paid_social"),
    fact("years_experience.seo", "6 years", fid="seo"),
    fact("years_experience.performance_marketing", 5, fid="performance_marketing"),
    fact("years_experience.direct_response_marketing", 8, fid="direct_response"),
    fact("years_experience.team_leadership", 4, fid="team_leadership"),
    fact("years_experience.content", "lots", fid="content_words"),
    fact("skills", "Meta Ads, Google Ads, SEO and content", source=RESUME, fid="skills"),
]


@pytest.mark.parametrize("question,expected", [
    ("Do you have experience with Meta Ads?", ["meta_ads"]),
    ("Have you run Instagram campaigns?", ["meta_ads"]),
    ("Do you have Facebook advertising experience?", ["meta_ads"]),
    ("Have you managed paid social?", ["meta_ads", "linkedin_ads", "paid_social"]),
    ("Do you have PPC experience?", ["google_ads", "paid_search"]),
    ("Have you managed Google AdWords accounts?", ["google_ads", "paid_search"]),
    ("Do you have SEM experience?", ["google_ads", "paid_search"]),
    ("Do you have search engine marketing experience?", ["google_ads", "paid_search"]),
    ("Have you run paid search campaigns?", ["google_ads", "paid_search"]),
    ("Do you have Google Analytics experience?", []),
    ("Do you have search engine optimisation experience?", ["seo"]),
    ("Do you have SEO AND GEO optimization experience?", ["seo"]),
    ("Have you advertised on LinkedIn?", ["linkedin_ads"]),
    ("Do you have 5+ years of paid media experience?", ["paid_media", "performance_marketing"]),
    ("Performance marketing experience?", ["paid_media", "performance_marketing"]),
    ("Do you have at least 8 years of total experience in direct response marketing?", ["direct_response"]),
    ("Do you have direct reports?", ["team_leadership"]),
    ("Have you managed a team of marketers?", ["team_leadership"]),
    ("Do you have people management experience?", ["team_leadership"]),
    ("Do you have at least 8 years of total experience?", []),
    ("Have you worked in Seoul?", []),
    ("Do you have content experience?", []),  # a years fact without a number is no area fact
])
def test_area_facts_are_the_years_facts_the_question_names_by_synonym_or_area_words(
    question: str, expected: list[str],
) -> None:
    assert ids(area_facts(question, AREA_FACTS)) == expected


def test_area_facts_read_any_iterable_and_never_return_the_total() -> None:
    facts = (f for f in AREA_FACTS)
    assert ids(area_facts("Do you have Meta and SEO experience?", facts)) == ["meta_ads", "seo"]
    assert area_facts("Do you have Meta experience?", []) == []


# --- platforms_named and platform_facts ---------------------------------------------------------

@pytest.mark.parametrize("option,named", [
    ("Google Ads", ["google_ads"]), ("Google AdWords", ["google_ads"]), ("AdWords", ["google_ads"]),
    ("Meta (Facebook/Instagram)", ["meta_ads"]), ("Facebook Ads", ["meta_ads"]), ("Instagram", ["meta_ads"]),
    ("LinkedIn Ads", ["linkedin_ads"]), ("Linked In", ["linkedin_ads"]),
    ("TikTok Ads", ["tiktok_ads"]), ("Tik Tok", ["tiktok_ads"]),
    ("Microsoft Advertising", ["microsoft_ads"]), ("Bing Ads", ["microsoft_ads"]),
    ("Amazon Ads", ["amazon_ads"]), ("Programmatic/DSP", ["programmatic"]), ("DV360", ["programmatic"]),
    ("The Trade Desk", ["programmatic"]), ("X (Twitter) Ads", ["x_ads"]), ("X Ads", ["x_ads"]),
    ("Snapchat", ["snapchat_ads"]), ("Snap Ads", ["snapchat_ads"]), ("Pinterest Ads", ["pinterest_ads"]),
    ("Reddit Ads", ["reddit_ads"]), ("YouTube", ["youtube_ads"]),
    ("Google Ads and Meta", ["google_ads", "meta_ads"]), ("Search (Google, Bing)", ["google_ads", "microsoft_ads"]),
    ("Google Analytics", []), ("Google Tag Manager", []), ("Google Sheets", []),
    ("Other", []), ("None of the above", []), ("Email marketing", []), ("", []),
])
def test_platforms_named_are_the_ad_platforms_an_option_names(option: str, named: list[str]) -> None:
    assert platforms_named(option) == named


# Found by the round-11 tests; fixed in round 11.
@pytest.mark.parametrize("option", ["Google Workspace", "Google Data Studio", "Google Cloud",
                                    "Microsoft Excel"])
def test_a_non_advertising_product_of_an_ad_vendor_names_no_platform(option: str) -> None:
    assert platforms_named(option) == []


@pytest.mark.parametrize("name,areas", [(name, areas) for name, _, areas in PLATFORMS])
def test_every_platform_is_stated_by_a_years_fact_for_each_of_its_areas(
    name: str, areas: tuple[str, ...],
) -> None:
    facts = [fact(f"years_experience.{area}", 3, fid=area) for area in areas]
    assert ids(platform_facts(name, facts)) == list(areas)
    assert name in areas  # the platform's own area is one of them


PLATFORM_FACTS = [
    fact("years_experience", 8, fid="total"),
    fact("years_experience.google_ads", 7, fid="google_years"),
    fact("years_experience.paid_search", "2 years", fid="paid_search_years"),
    fact("years_experience.meta_ads", 0.5, fid="meta_half_year"),
    fact("years_experience.seo", 6, fid="seo_years"),
    fact("years_experience.tiktok_ads", 0, fid="tiktok_zero"),
    fact("experience", "I ran Google Ads and Meta campaigns for Fictional Spark Media clients.",
         source=STORY, fid="story_google_meta"),
    fact("experience", "I launched our first TikTok Ads tests at Fictional Rank Works.",
         source=USER_STORY, fid="user_story_tiktok"),
    fact("experience", "I built Google Analytics dashboards for Fictional Rank Works.",
         source=STORY, fid="story_analytics"),
    fact("experience", "Managed Microsoft Advertising and Amazon Ads budgets at Fictional Widgets Co.",
         source=RESUME, fid="resume_microsoft_amazon"),
    fact("years_experience.linkedin_ads", 3, source=DERIVED, fid="linkedin_derived"),
]


@pytest.mark.parametrize("platform,expected", [
    ("google_ads", ["google_years", "paid_search_years", "story_google_meta"]),
    ("meta_ads", ["story_google_meta"]),  # half a year is not a year of Meta
    ("tiktok_ads", ["user_story_tiktok"]),  # the stated story, not a zero-year fact
    ("linkedin_ads", ["linkedin_derived"]),
    ("microsoft_ads", []),  # a resume bullet is neither a years fact nor a story
    ("amazon_ads", []),
    ("programmatic", []),
])
def test_platform_facts_are_the_years_and_story_facts_stating_the_platform(
    platform: str, expected: list[str],
) -> None:
    assert ids(platform_facts(platform, PLATFORM_FACTS)) == expected


# Found by the round-11 tests; fixed in round 11.
def test_a_story_naming_a_platform_the_person_did_not_manage_is_no_platform_fact() -> None:
    story = fact("experience", "TikTok Ads were run by a partner team; I did not manage them.",
                 source=STORY, fid="story_not_tiktok")
    assert platform_facts("tiktok_ads", [story]) == []


# --- experience_wording -----------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "Have you owned paid social strategy and execution across multiple platforms?",
    "Have you led client-facing conversations, such as performance readouts, QBRs and strategy reviews?",
    "Do you have SEO AND GEO optimization experience?",
    "Do you have hands-on experience managing paid campaigns across Meta/Instagram and Search?",
    "Which paid media platforms have you directly managed?",
    "Have you worked in a performance marketing agency environment?",
    "Do you have at least 8 years of total experience?",
    "Do you have 5+ years in SaaS?",
    "Are you proficient in SQL?",
    "Are you familiar with Fictional CRM Pro?",
    "Which of these skills do you have?",
    "Describe your expertise in attribution.",
    "Have you launched a product?",
    "Have you built server-side tagging?",
    "Have you optimised bidding strategies?",
    "Have you implemented offline conversion imports?",
    "Are you an experienced people leader?",
])
def test_experience_wording_is_a_question_about_experience_skills_platforms_years_or_done(
    question: str,
) -> None:
    assert experience_wording(question)


@pytest.mark.parametrize("question", [
    "Are you willing to relocate?",
    "Do you require visa sponsorship now or in the future?",
    "Are you legally authorized to work in the United States?",
    "What is your desired salary?",
    "How did you hear about us?",
    "Are you available to start within 30 days?",
    "Can you work on-site in Austin?",
    "Do you consent to a background check?",
    "What is your LinkedIn URL?",
    "Do you have a driver's license?",
    "Are you comfortable working from a fictional office?",
    "",
])
def test_experience_wording_leaves_eligibility_preferences_and_contact_questions_out(
    question: str,
) -> None:
    assert not experience_wording(question)
