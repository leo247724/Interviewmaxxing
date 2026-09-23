from __future__ import annotations

import pytest

from interviewmaxxing_core import CompensationPeriod, WorkArrangement
from interviewmaxxing_jobs.text import (
    compensation_from_schema,
    country_level_region,
    decode_linkedin_redirect,
    employer_key_from_url,
    html_to_text,
    known_source_ref,
    parse_arrangement,
    parse_compensation,
    pay_segment,
    split_indeed_location,
    split_linkedin_caption,
)


@pytest.mark.parametrize(
    ("text", "low", "high", "period"),
    [
        ("$110K/yr - $130K/yr · 3 benefits", 110_000, 130_000, CompensationPeriod.YEAR),
        ("$55,000 - $68,000 a year", 55_000, 68_000, CompensationPeriod.YEAR),
        ("$50 - $60 an hour", 50, 60, CompensationPeriod.HOUR),
        ("Up to $150,000 a year", None, 150_000, CompensationPeriod.YEAR),
        ("From $120,000 a year", 120_000, None, CompensationPeriod.YEAR),
        ("$9,500 a month", 9_500, 9_500, CompensationPeriod.MONTH),
    ],
)
def test_stated_pay_gets_bounds_only_with_currency_and_period(
    text: str, low: float | None, high: float | None, period: CompensationPeriod
) -> None:
    pay = parse_compensation(text, dollar_currency="USD")
    assert pay is not None and pay.is_comparable
    assert (pay.minimum, pay.maximum, pay.currency, pay.period) == (low, high, "USD", period)


@pytest.mark.parametrize(
    "text",
    [
        "106K-167K Annually",  # Built In card: no currency shown
        "121K\u2013194K a year",  # Google chip (en dash): no currency shown
        "Estimated $140K - $170K a year",  # an estimate, not stated by the employer
        "$120,000",  # no period
        "Competitive salary",
    ],
)
def test_unstated_or_estimated_pay_keeps_only_raw_text(text: str) -> None:
    pay = parse_compensation(text, dollar_currency="USD")
    assert pay is not None
    assert pay.raw_text == text
    assert not pay.is_comparable


def test_dollar_sign_is_not_a_currency_unless_the_source_says_so() -> None:
    pay = parse_compensation("$110K/yr - $130K/yr")
    assert pay is not None and not pay.is_comparable


def test_pay_segment_ignores_benefit_counts() -> None:
    assert pay_segment("$140K/yr - $170K/yr · 401(k) benefit") == "$140K/yr - $170K/yr"
    assert pay_segment("401(k) benefit") is None
    assert parse_compensation(None) is None


def test_schema_salary_uses_explicit_unit_and_currency() -> None:
    pay = compensation_from_schema({"currency": "USD", "value": {
        "minValue": 106200, "maxValue": 166850, "unitText": "YEAR"}})
    assert pay is not None
    assert (pay.minimum, pay.maximum, pay.currency, pay.period) == (
        106_200, 166_850, "USD", CompensationPeriod.YEAR)
    assert compensation_from_schema({"currency": "USD", "value": {"minValue": 1}}) is None
    assert compensation_from_schema(None) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Hybrid", WorkArrangement.HYBRID),
        ("On-site", WorkArrangement.ONSITE),
        ("Fully Remote", WorkArrangement.REMOTE),
        ("Work from home", WorkArrangement.REMOTE),
        ("In-Office or Remote", WorkArrangement.UNKNOWN),
        ("Remote or Hybrid", WorkArrangement.UNKNOWN),
        ("Senior Marketing Manager", WorkArrangement.UNKNOWN),
        (None, WorkArrangement.UNKNOWN),
    ],
)
def test_arrangement_only_from_an_explicit_single_label(
    label: str | None, expected: WorkArrangement
) -> None:
    assert parse_arrangement(label) is expected


def test_location_labels() -> None:
    assert split_linkedin_caption("Austin, Texas Metropolitan Area (Hybrid)") == (
        "Austin, Texas Metropolitan Area", WorkArrangement.HYBRID)
    assert split_linkedin_caption("Austin, TX") == ("Austin, TX", WorkArrangement.UNKNOWN)
    assert split_indeed_location("Remote in Austin, TX") == ("Austin, TX", WorkArrangement.REMOTE)
    assert split_indeed_location("Hybrid work in Austin, TX 78701") == (
        "Austin, TX 78701", WorkArrangement.HYBRID)
    assert split_indeed_location("Remote") == (None, WorkArrangement.REMOTE)
    assert split_indeed_location("Round Rock, TX") == ("Round Rock, TX", WorkArrangement.UNKNOWN)
    assert country_level_region("United States") == "United States"
    assert country_level_region("Greater Orlando") is None


def test_linkedin_redirect_and_source_refs() -> None:
    assert decode_linkedin_redirect(
        "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fats.example.test%2Fjobs%2F1%3Fa%3Db&urlhash=x"
    ) == "https://ats.example.test/jobs/1?a=b"
    assert decode_linkedin_redirect("https://ats.example.test/x") == "https://ats.example.test/x"
    assert known_source_ref("https://www.linkedin.com/jobs/view/some-title-at-co-4000000001?trk=x") == (
        "linkedin", "4000000001", "https://www.linkedin.com/jobs/view/4000000001/")
    assert known_source_ref("https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718&utm_source=g") == (
        "indeed", "a1b2c3d4e5f60718", "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718")
    assert known_source_ref("https://builtin.com/job/growth-marketing-manager/9000001") == (
        "builtin", "9000001", "https://builtin.com/job/growth-marketing-manager/9000001")
    assert known_source_ref("https://jobs.example-board.test/view/1") is None
    assert known_source_ref("https://www.indeed.com/viewjob?jk=not-a-key") is None


def test_html_description_to_text() -> None:
    text = html_to_text("<strong>About</strong><br>We build.<ul><li>One</li><li>Two &amp; three</li></ul>"
                        "<script>ignored()</script><p>End</p>")
    assert text == "About\nWe build.\n\n- One\n- Two & three\n\nEnd"
    assert html_to_text("   ") is None


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://boards.greenhouse.io/fictionalwidgets/jobs/7001?gh_src=x",
         "ats:greenhouse:fictionalwidgets:7001"),
        ("https://job-boards.greenhouse.io/fictionalwidgets/jobs/7001",
         "ats:greenhouse:fictionalwidgets:7001"),
        ("https://boards.greenhouse.io/embed/job_app?for=fictionalwidgets&token=7001",
         "ats:greenhouse:fictionalwidgets:7001"),
        ("https://jobs.lever.co/samplecoffee/0f8fad5b-d9cb-469f-a165-70867728950e/apply",
         "ats:lever:samplecoffee:0f8fad5b-d9cb-469f-a165-70867728950e"),
        ("https://jobs.ashbyhq.com/Example/0f8fad5b-d9cb-469f-a165-70867728950e",
         "ats:ashby:example:0f8fad5b-d9cb-469f-a165-70867728950e"),
        ("https://fictional.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Marketing-Manager_R12345",
         "ats:workday:fictional:r12345"),
        # Generic endpoints, careers home pages and boards without a job id are not keys.
        ("https://boards.greenhouse.io/fictionalwidgets", None),
        ("https://fictional.wd5.myworkdayjobs.com/External", None),
        ("https://careers.fictional.test/apply", None),
        ("https://www.linkedin.com/jobs/view/4000000001/", None),
        (None, None),
    ],
)
def test_employer_job_key_only_from_job_specific_ats_urls(url: str | None, key: str | None) -> None:
    assert employer_key_from_url(url) == key
