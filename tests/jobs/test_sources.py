from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest

from interviewmaxxing_core import (
    CompensationPeriod,
    DescriptionCompleteness,
    JobListing,
    JobSearchQuery,
    ListingStatus,
    LocationPriority,
    SourceSearchState,
    WorkArrangement,
)
from interviewmaxxing_jobs.sources import (
    AccessProblem,
    BuiltInAdapter,
    GoogleAdapter,
    IndeedAdapter,
    LinkedInAdapter,
    SearchContext,
    SourceAdapter,
    builtin,
    google,
    indeed,
    linkedin,
)
from interviewmaxxing_jobs.sources.base import (
    MAX_BATCHES_PER_TARGET,
    PLAIN,
    BudgetPlan,
    SearchLeg,
    build_legs,
    leg_budgets,
    plan_note,
)

from .conftest import FakeTransport, fixture_routes

Clock = Callable[[], datetime]


def austin_manager_query(**overrides: object) -> JobSearchQuery:
    base: dict[str, object] = {"title_phrases": ["marketing manager"], "remote": None,
                               "max_results_per_source": 5}
    return JobSearchQuery.model_validate({**base, **overrides})


def run(adapter: SourceAdapter, clock: Clock, *, query: JobSearchQuery | None = None,
        detail_limit: int = 5, overrides: dict[str, str] | None = None
        ) -> tuple[list[JobListing], FakeTransport, SourceSearchState, str | None]:
    transport = FakeTransport(fixture_routes(overrides))
    ctx = SearchContext(transport=transport, session=f"imx-jobs-{adapter.name}",
                        query=query or austin_manager_query(),
                        limit=(query or austin_manager_query()).max_results_per_source,
                        detail_limit=detail_limit, clock=clock, profile="test-profile")
    outcome = adapter.search(ctx)
    return [o.listing for o in outcome.observations], transport, outcome.state, outcome.message


def by_title(listings: list[JobListing]) -> dict[str, JobListing]:
    return {x.title: x for x in listings}


# --- search plans -------------------------------------------------------------------


SEEDS = ["paid media manager", "senior paid media manager", "performance marketing manager",
         "growth marketing manager", "demand generation manager", "digital marketing manager",
         "marketing manager", "marketing director"]


def test_default_seeds_are_the_performance_marketing_titles_in_preferred_order() -> None:
    query = JobSearchQuery()
    assert query.title_phrases == SEEDS
    assert "not an exact job-title match" in query.role_focus
    assert query.location_priority is LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID


def test_plain_sources_search_each_seed_austin_first_within_a_bounded_budget() -> None:
    query = JobSearchQuery()
    legs = build_legs(query)  # Indeed / Built In: one seed per search
    assert len(legs) == 16
    assert [(leg.text, leg.target) for leg in legs[:8]] == [(s, "onsite") for s in SEEDS]
    assert [(leg.text, leg.target) for leg in legs[8:]] == [(s, "remote") for s in SEEDS]
    assert legs[0].arrangements == (WorkArrangement.ONSITE, WorkArrangement.HYBRID)
    assert legs[8].arrangements == (WorkArrangement.REMOTE,)
    budgets = BudgetPlan(50, legs, query.location_priority).budgets
    assert sum(budgets) == 50 and budgets[:8] == [6, 6, 5, 5, 5, 5, 5, 5] and budgets[8:] == [1] * 8
    # A small limit reaches the most preferred Austin seeds first; remote only by carry.
    assert BudgetPlan(3, legs, query.location_priority).budgets == [1, 1, 1] + [0] * 13


def test_linkedin_and_google_batch_seeds_with_or() -> None:
    query = JobSearchQuery()
    li = build_legs(query, linkedin.SYNTAX)
    assert [leg.target for leg in li] == ["onsite", "onsite", "remote", "remote"]
    assert li[0].title_phrases == tuple(SEEDS[:4])
    assert li[0].text == ("(paid media manager) OR (senior paid media manager) OR "
                          "(performance marketing manager) OR (growth marketing manager)")
    assert '"' not in li[0].text  # quoted phrases act as exact-title filters on LinkedIn
    g = build_legs(query, google.SYNTAX)
    assert len(g) == 6
    assert google.search_text(g[0]) == (
        "paid media manager OR senior paid media manager OR performance marketing manager "
        "jobs in Austin, TX")
    assert google.search_text(g[3]).startswith("remote paid media manager OR ")
    keyed = build_legs(JobSearchQuery(title_phrases=SEEDS[:2], keywords=["B2B"], remote=None),
                       linkedin.SYNTAX)
    assert keyed[0].text == "((paid media manager) OR (senior paid media manager)) B2B"


def test_many_seeds_stay_bounded_and_the_rest_are_reported() -> None:
    seeds = [f"fictional seed {i}" for i in range(12)]
    query = JobSearchQuery(title_phrases=seeds, remote=None)
    assert len(build_legs(query)) == MAX_BATCHES_PER_TARGET
    note = plan_note(query, PLAIN)
    assert note and "4 title seeds" in note and "fictional seed 11" in note
    assert len(build_legs(query, linkedin.SYNTAX)) == 3
    assert plan_note(query, linkedin.SYNTAX) is None


def test_other_location_priorities_reorder_but_keep_every_leg() -> None:
    seeds = ["marketing manager", "marketing director"]
    balanced = build_legs(JobSearchQuery(title_phrases=seeds, location_priority=LocationPriority.BALANCED))
    assert [leg.target for leg in balanced] == ["onsite", "remote", "onsite", "remote"]
    remote_first = build_legs(JobSearchQuery(title_phrases=seeds,
                                             location_priority=LocationPriority.PREFER_REMOTE))
    assert [leg.target for leg in remote_first] == ["remote", "remote", "onsite", "onsite"]
    assert leg_budgets(50, 4) == [13, 13, 12, 12]


def test_unused_austin_budget_carries_forward_to_remote() -> None:
    legs = build_legs(JobSearchQuery(title_phrases=["marketing manager", "marketing director"]))
    plan = BudgetPlan(10, legs, LocationPriority.STRONGLY_PREFER_ONSITE_HYBRID)
    assert plan.budgets == [4, 4, 1, 1]
    plan.spent(0, 1)  # thin Austin market for the first seed
    assert plan.budget(1) == 7
    plan.spent(1, 7)
    assert plan.budget(2) == 1


def test_results_are_not_filtered_to_seed_titles(clock: Clock) -> None:
    # A paid-media search keeps every role the source returned, e.g. a "Director of
    # Marketing" or "Growth Marketing Manager": eligibility is judged semantically later.
    query = austin_manager_query(title_phrases=["paid media manager"])
    listings, _, _, _ = run(BuiltInAdapter(), clock, query=query)
    assert {x.title for x in listings} == {"Growth Marketing Manager", "Marketing Director"}


def test_custom_keywords_join_the_search_text() -> None:
    legs = build_legs(JobSearchQuery(title_phrases=["brand manager"], keywords=["B2B", "SaaS"],
                                     remote=None))
    assert [leg.text for leg in legs] == ["brand manager B2B SaaS"]


def test_source_search_urls_follow_the_observed_ui() -> None:
    onsite, remote = build_legs(JobSearchQuery(title_phrases=["marketing manager"]))
    assert (onsite.target, remote.target) == ("onsite", "remote")
    assert linkedin.search_url(onsite) == (
        "https://www.linkedin.com/jobs/search/?keywords=marketing+manager&location=Austin%2C+TX&f_WT=1%2C3")
    assert linkedin.search_url(remote, start=25) == (
        "https://www.linkedin.com/jobs/search/?keywords=marketing+manager&location=United+States"
        "&f_WT=2&start=25")
    assert builtin.search_url(onsite) == (
        "https://builtin.com/jobs/hybrid/office?search=marketing+manager&city=Austin&state=Texas"
        "&country=USA&allLocations=true")
    assert builtin.search_url(remote, page=2) == (
        "https://builtin.com/jobs/remote?search=marketing+manager&city=&state=&country=USA"
        "&allLocations=true&page=2")
    assert indeed.search_url(onsite) == "https://www.indeed.com/jobs?q=marketing+manager&l=Austin%2C+TX"
    assert indeed.search_url(remote, start=10, posted_within_days=5) == (
        "https://www.indeed.com/jobs?q=marketing+manager&l=Remote&fromage=7&start=10")
    assert google.search_url(onsite) == (
        "https://www.google.com/search?q=marketing+manager+jobs+in+Austin%2C+TX&udm=8")
    assert google.search_text(remote) == "remote marketing manager jobs United States"


def test_remote_outside_the_us_is_not_rewritten_as_a_us_search() -> None:
    leg = SearchLeg("marketing manager", ("marketing manager",), "remote", "Canada",
                    (WorkArrangement.REMOTE,))
    assert builtin.search_url(leg) is None
    assert indeed.search_url(leg) is None


# --- LinkedIn -----------------------------------------------------------------------


def test_linkedin_reads_cards_and_job_pages(clock: Clock) -> None:
    listings, transport, state, _ = run(LinkedInAdapter(), clock, detail_limit=2)
    found = by_title(listings)
    assert set(found) == {"Marketing Manager", "Director of Marketing", "Senior Marketing Manager"}

    manager = found["Marketing Manager"]
    assert manager.source_listing_id == "4000000001"
    assert manager.posting_url == "https://www.linkedin.com/jobs/view/4000000001/"
    assert manager.source_url == "https://www.linkedin.com/jobs/view/4000000001/"
    assert manager.company == "Fictional Widgets Co"
    assert manager.location == "Austin, TX"
    assert manager.work_arrangement is WorkArrangement.HYBRID
    assert manager.posted_text == "2 days ago"
    assert manager.status is ListingStatus.OPEN
    assert manager.application_url == "https://boards.greenhouse.io/fictionalwidgets/jobs/7001?gh_src=linkedin"
    assert manager.provenance[0].employer_job_key == "ats:greenhouse:fictionalwidgets:7001"
    assert "read from https://boards.greenhouse.io/fictionalwidgets/jobs/7001" in manager.evidence
    pay = manager.compensation
    assert pay is not None and (pay.minimum, pay.maximum, pay.currency, pay.period) == (
        110_000, 130_000, "USD", CompensationPeriod.YEAR)
    assert manager.description_completeness is DescriptionCompleteness.NONE
    assert "not rendered" in manager.evidence

    # An unrendered card is read from its job page; closed postings are marked closed.
    closed = found["Senior Marketing Manager"]
    assert closed.status is ListingStatus.CLOSED
    assert closed.work_arrangement is WorkArrangement.ONSITE
    assert closed.description == "About the role\nLead fictional campaigns for a sample roaster."
    assert closed.description_completeness is DescriptionCompleteness.FULL

    # Card-only listing: pay-less metadata is not pay; remote US eligibility as stated.
    director = found["Director of Marketing"]
    # A card seen on a results page: the page is provenance, the job URL is identity.
    assert director.source_url.startswith("https://www.linkedin.com/jobs/search/")
    assert director.posting_url == "https://www.linkedin.com/jobs/view/4000000002/"
    assert director.compensation is None
    assert director.work_arrangement is WorkArrangement.REMOTE
    assert director.remote_eligibility == "United States"
    assert director.status is ListingStatus.UNKNOWN
    assert state is SourceSearchState.OK
    assert not any(c[0] == "click" for c in transport.calls)


def test_linkedin_unrendered_cards_beyond_the_detail_budget_make_the_result_partial(clock: Clock) -> None:
    listings, _, state, message = run(LinkedInAdapter(), clock, detail_limit=0)
    assert {x.title for x in listings} == {"Marketing Manager", "Director of Marketing"}
    assert state is SourceSearchState.PARTIAL
    assert message and "not rendered" in message


def test_linkedin_sign_in_wall_is_needs_user(clock: Clock) -> None:
    with pytest.raises(AccessProblem) as err:
        run(LinkedInAdapter(), clock, overrides={"linkedin": "login_wall"})
    assert err.value.state is SourceSearchState.NEEDS_USER
    assert err.value.user_action and "imx-jobs-linkedin" in err.value.user_action
    assert "--profile test-profile" in err.value.user_action


# --- Built In -----------------------------------------------------------------------


def test_builtin_uses_the_job_posting_data(clock: Clock) -> None:
    listings, _, state, _ = run(BuiltInAdapter(), clock)
    found = by_title(listings)
    growth = found["Growth Marketing Manager"]
    assert growth.source_listing_id == "9000001"
    assert growth.posting_url == "https://builtin.com/job/growth-marketing-manager/9000001"
    # The card's mixed label is kept UNKNOWN even though the posting is TELECOMMUTE.
    assert growth.work_arrangement is WorkArrangement.UNKNOWN
    assert growth.remote_eligibility == "USA"
    pay = growth.compensation
    assert pay is not None and (pay.minimum, pay.maximum, pay.currency, pay.period) == (
        106_200, 166_850, "USD", CompensationPeriod.YEAR)
    assert pay.raw_text == "106K-167K Annually"
    assert growth.description is not None and "- Report on pipeline & revenue" in growth.description
    assert growth.description_completeness is DescriptionCompleteness.FULL
    assert growth.status is ListingStatus.OPEN
    assert growth.application_url is None  # Built In's redirect is not followed
    assert found["Marketing Director"].status is ListingStatus.CLOSED  # validThrough passed
    assert state is SourceSearchState.OK


def test_builtin_card_only_pay_without_currency_stays_raw(clock: Clock) -> None:
    listings, _, _, _ = run(BuiltInAdapter(), clock, detail_limit=0)
    growth = by_title(listings)["Growth Marketing Manager"]
    assert growth.compensation is not None and not growth.compensation.is_comparable
    assert growth.description_completeness is DescriptionCompleteness.NONE


# --- Indeed -------------------------------------------------------------------------


def test_indeed_cards_and_job_pages(clock: Clock) -> None:
    listings, _, state, _ = run(IndeedAdapter(), clock)
    found = by_title(listings)
    manager = found["Marketing Manager"]
    assert manager.posting_url == "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718"
    assert manager.provenance[0].employer_job_key is None  # a careers page is not a job key
    assert manager.location == "Austin, TX 78701"
    assert manager.work_arrangement is WorkArrangement.HYBRID
    assert manager.application_url == "https://careers.sample-coffee.test/jobs/55"
    assert manager.status is ListingStatus.OPEN
    assert manager.description == "Description\n\nGrow a fictional coffee brand."
    pay = manager.compensation
    assert pay is not None and (pay.minimum, pay.maximum) == (95_000, 120_000)

    director = found["Marketing Director"]
    assert director.status is ListingStatus.CLOSED
    assert director.description is None  # the "expired" notice is not a description
    assert director.work_arrangement is WorkArrangement.REMOTE
    assert director.compensation is not None and not director.compensation.is_comparable
    assert state is SourceSearchState.OK


def test_indeed_challenge_is_needs_user_not_empty(clock: Clock) -> None:
    with pytest.raises(AccessProblem) as err:
        run(IndeedAdapter(), clock, overrides={"indeed": "challenge"})
    assert err.value.state is SourceSearchState.NEEDS_USER
    assert "not solved automatically" in err.value.message


# --- Google -------------------------------------------------------------------------


def test_google_results_are_identified_by_google_and_proven_only_by_ats_keys(clock: Clock) -> None:
    transport = FakeTransport(fixture_routes())
    ctx = SearchContext(transport=transport, session="imx-jobs-google", query=austin_manager_query(),
                        limit=5, detail_limit=5, clock=clock, profile="test-profile")
    outcome = GoogleAdapter().search(ctx)
    found = {o.listing.title: o for o in outcome.observations}
    manager = found["Marketing Manager"].listing
    assert manager.source == "google"
    assert manager.source_listing_id == "RmljdGlvbmFsRG9jMDAx=="
    assert manager.posting_url is not None and "htidocid=RmljdGlvbmFsRG9jMDAx" in manager.posting_url
    assert manager.source_url.startswith("https://www.google.com/search?q=marketing+manager")
    assert manager.location == "Austin, TX"
    assert manager.description == "Lead fictional marketing programs.\nOwn the widget launch calendar."
    assert manager.description_completeness is DescriptionCompleteness.PARTIAL
    # Links to LinkedIn/Indeed postings are evidence only, never identity.
    assert [p.source for p in manager.provenance] == ["google"]
    assert {x["source"] for x in found["Marketing Manager"].raw["linked_postings"]} == {
        "linkedin", "indeed"}
    assert manager.provenance[0].employer_job_key == "ats:greenhouse:fictionalwidgets:7001"
    assert manager.application_url == "https://boards.greenhouse.io/fictionalwidgets/jobs/7001?gh_src=google_jobs"

    brand = found["Brand & Content Marketing Manager"].listing
    assert brand.provenance[0].employer_job_key is None
    assert brand.work_arrangement is WorkArrangement.REMOTE
    assert brand.compensation is not None and not brand.compensation.is_comparable
    assert brand.description_completeness is DescriptionCompleteness.FULL
    assert [c for c in transport.calls if c[0] == "click"] == [
        ("click", "imx-jobs-google", "[data-share-url]", "0"),
        ("click", "imx-jobs-google", "[data-share-url]", "1")]


def test_google_unusual_traffic_page_is_needs_user(clock: Clock) -> None:
    with pytest.raises(AccessProblem) as err:
        run(GoogleAdapter(), clock, overrides={"google": "sorry"})
    assert err.value.state is SourceSearchState.NEEDS_USER


def test_google_logo_initial_is_not_taken_as_the_title() -> None:
    items = google.parse_items({"page": {"url": "https://www.google.com/search?q=x&udm=8"}, "items": [
        {"index": 0, "doc_id": "RmljdGlvbmFs==",
         "lines": ["S", "Senior Marketing Director", "Sample Coffee Roasters",
                   "Austin, TX • via Example Jobs Board", "3 days ago"]}]})
    assert (items[0].title, items[0].company, items[0].location) == (
        "Senior Marketing Director", "Sample Coffee Roasters", "Austin, TX")
