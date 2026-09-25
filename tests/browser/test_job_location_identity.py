"""Round 14 (WP2): the location a posting page states for its job.

On the prepare-batch path the job's location reaches ``PacketContext.job`` only through the
page: ``extract_job_identity`` reads the schema.org ``JobPosting`` of the page, and the runner
binds its ``location`` to the job (``ApplicationStore.bind_job_identity``). The metro rule
(round 13) reads it, and a job without one reads as remote. Fictional pages only (Mock Co)."""
from __future__ import annotations

import json
from typing import Any

import pytest

from interviewmaxxing_browser.normalize import extract_job_identity
from interviewmaxxing_browser.snapshot import DomHeading, DomMeta, DomSnapshot

PAGE = "https://jobs.example.test/mock-co/4012"


def posting(job_location: Any) -> str:
    return json.dumps({"@context": "https://schema.org", "@type": "JobPosting",
                       "title": "Senior Paid Media Manager", "identifier": "4012",
                       "hiringOrganization": {"@type": "Organization", "name": "Mock Co"},
                       "jobLocation": job_location})


def snapshot(*ld_json: str) -> DomSnapshot:
    return DomSnapshot(
        url=PAGE, title="Senior Paid Media Manager | Mock Co",
        headings=[DomHeading(level=1, text="Senior Paid Media Manager")], regions=[],
        body_text="Senior Paid Media Manager at Mock Co. Round Rock, TX (Hybrid).", record_members=[],
        ld_json=list(ld_json), meta=DomMeta(og_site_name="Mock Co", og_title="Senior Paid Media Manager"),
        forms=[], controls=[], buttons=[], links=[], step=None, password_visible=False,
        captcha_frames=[], captcha_tokens=[], captcha_widget=False, document=f"1758700000000 {PAGE}",
        dialogs=[], progress=[], frames=[])


ROUND_ROCK = {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": "Round Rock",
                                             "addressRegion": "TX", "addressCountry": "US"}}


def test_a_single_job_location_gives_its_locality() -> None:
    identity = extract_job_identity(snapshot(posting(ROUND_ROCK)))
    assert identity is not None and identity.external_job_id == "4012"
    # Only the locality is kept (the region and country are dropped): the metro rule still
    # reads "Round Rock" as in the metro, since no other state follows it.
    assert identity.location == "Round Rock"


@pytest.mark.xfail(strict=True, reason=(
    "WP1 (normalize.py extract_job_identity): a JobPosting whose jobLocation is a list of places "
    "(schema.org allows one or more) gives no location, because only a dict is read; the job "
    "then reads as remote for the metro rule. Expected the first place's locality"))
def test_a_list_of_job_locations_gives_the_first_locality() -> None:
    identity = extract_job_identity(snapshot(posting([ROUND_ROCK])))
    assert identity is not None and identity.location == "Round Rock"


def test_a_page_showing_only_a_job_id_gives_no_location() -> None:
    # The mock ATS's apply page ("… · Round Rock, TX (Hybrid) · Job ID 4012" in its heading) has no
    # JSON-LD: the identity comes from the page text and carries no location.
    page = snapshot().model_copy(update={"headings": [DomHeading(
        level=1, text="Senior Paid Media Manager · Round Rock, TX (Hybrid) · Job ID 4012")]})
    identity = extract_job_identity(page)
    assert identity is not None and identity.external_job_id == "4012" and identity.location is None
