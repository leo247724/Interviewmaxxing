"""Round 15, item 6: the location a posting states for its job.

``extract_job_identity`` reads a ``JobPosting``'s ``jobLocation`` whether it is one place or a
list of places, and keeps each place's region and country beside its locality ("Austin, TX,
US"), so the metro rule (WP2) can tell Austin, TX from Austin, MN. A form page whose own
identity ("Job ID …" in its text) states no location keeps the posting's. Fictional pages only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from interviewmaxxing_browser import PlaywrightSessionFactory
from interviewmaxxing_browser.ai.metro import Metro, metro_place_named
from interviewmaxxing_browser.normalize import extract_job_identity
from interviewmaxxing_browser.snapshot import DomHeading, DomMeta, DomSnapshot
from interviewmaxxing_core import BrowserOptions, IdentityEvidenceKind, PageKind

PAGE = "https://jobs.example.test/mock-co/4012"


def _snapshot(job_location: Any) -> DomSnapshot:
    posting = json.dumps({"@context": "https://schema.org", "@type": "JobPosting", "title": "Paid Media Manager",
                          "identifier": "4012", "hiringOrganization": {"@type": "Organization", "name": "Mock Co"},
                          "jobLocation": job_location})
    return DomSnapshot(
        url=PAGE, title="Paid Media Manager | Mock Co", headings=[DomHeading(level=1, text="Paid Media Manager")],
        regions=[], body_text="Paid Media Manager at Mock Co.", record_members=[], ld_json=[posting],
        meta=DomMeta(og_site_name="Mock Co", og_title="Paid Media Manager"), forms=[], controls=[], buttons=[],
        links=[], step=None, password_visible=False, captcha_frames=[], captcha_tokens=[], captcha_widget=False,
        document=f"1758700000000 {PAGE}", dialogs=[], progress=[], frames=[])


def _place(locality: str, region: str | None = None, country: Any = None) -> dict[str, Any]:
    address: dict[str, Any] = {"@type": "PostalAddress", "addressLocality": locality}
    if region:
        address["addressRegion"] = region
    if country:
        address["addressCountry"] = country
    return {"@type": "Place", "address": address}


@pytest.mark.parametrize(("job_location", "expected"), [
    (_place("Austin", "TX", "US"), "Austin, TX, US"),
    ([_place("Austin", "TX", "US")], "Austin, TX, US"),
    ([_place("Austin", "TX", "US"), _place("Denver", "CO", {"@type": "Country", "name": "US"})],
     "Austin, TX, US / Denver, CO, US"),
    ([_place("Austin", "TX", "US"), _place("Austin", "TX", "US")], "Austin, TX, US"),  # each place once
    (_place("Singapore", None, "Singapore"), "Singapore"),  # a part stated twice, once
    (_place("Remote (US)"), "Remote (US)"),
    ({"@type": "Place", "address": "Round Rock, TX"}, "Round Rock, TX"),
    ([], None),
    (None, None),
], ids=["one-place", "list-of-one", "list-of-two", "repeated", "city-state", "locality-only", "text-address",
        "empty-list", "none"])
def test_a_job_location_keeps_region_and_country_and_reads_lists(job_location: Any, expected: str | None) -> None:
    identity = extract_job_identity(_snapshot(job_location))
    assert identity is not None and identity.location == expected


def test_the_metro_rule_tells_austin_texas_from_austin_minnesota() -> None:
    metro = Metro(city="Austin", state="TX", places=("Austin", "Round Rock"))
    assert metro_place_named(extract_job_identity(_snapshot(_place("Austin", "TX", "US"))).location or "", metro)
    assert not metro_place_named(extract_job_identity(_snapshot(_place("Austin", "MN", "US"))).location or "",
                                 metro)


def test_a_form_with_its_own_job_id_keeps_the_postings_location(
    kit: Any, server: Any, options: BrowserOptions
) -> None:
    async def scenario() -> Any:
        browser = await PlaywrightSessionFactory().start(options)
        try:
            return await browser.open(server.url("/jobs/standard"))
        finally:
            await browser.close()

    page = kit.run(scenario())
    assert page.kind is PageKind.APPLICATION_FORM, page.message
    identity = page.job_identity
    # The form page's own identity (its "Job ID" text) with the posting's JSON-LD location.
    assert identity is not None and identity.evidence_kind is IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE
    assert (identity.external_job_id, identity.location) == ("BWA-ENG-101", "Denver, CO (Hybrid)")
