"""Deterministic, fictional listing population and question catalog.

Everything is invented: companies, tenants, job ids, descriptions, the candidate.
Domains use ``.example``. The records follow the D0 ``JobListing`` JSON shape closely
enough that ``imx_perf.measure`` validates them against the real contract when the
core package is importable. Each listing also carries a private ``truth`` block that
only the simulations use to drive a *stub* judge; it says nothing about how Jev would
answer.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import Assumptions

OBSERVED_AT = "2026-09-22T21:00:00Z"

COMPANIES = [
    "Fictional Co", "Example Labs", "Placeholder Systems", "Sample Goods", "Mock Mobility",
    "Demo Health", "Testcase Financial", "Lorem Logistics", "Ipsum Interactive", "Stub Studios",
    "Dummy Devices", "Fixture Foods", "Sandbox Software", "Hypothetical Hotels", "Nominal Networks",
    "Prototype Pets", "Specimen Sports", "Template Travel", "Trial Textiles", "Vector Ventures",
]

ATS_TYPES = ["greenhouse", "lever", "ashby", "smartrecruiters", "workday", "generic"]

TITLES = {
    "match": [
        "Paid Media Manager", "Senior Paid Media Manager", "Performance Marketing Manager",
        "Growth Marketing Manager", "Demand Generation Manager", "Digital Marketing Manager",
        "Head of Growth", "Acquisition Lead", "Director of Performance Marketing",
        "Senior Manager, Paid Acquisition", "Paid Search Manager", "Lifecycle and Paid Media Lead",
    ],
    "adjacent": [
        "Marketing Manager", "Brand Marketing Manager", "Content Marketing Manager",
        "Product Marketing Manager", "Field Marketing Manager", "Events Marketing Manager",
        "Marketing Operations Manager", "Communications Manager",
    ],
    "mismatch": [
        "Senior Data Platform Engineer", "Marketing Analytics Engineer", "Sales Development Representative",
        "Software Engineer, Ads Platform", "Account Executive", "Recruiting Coordinator",
        "Data Scientist, Marketing", "Customer Success Manager",
    ],
}

SENIORITY_BY_TITLE_HINT = {
    "Senior": "at_target_level", "Director": "at_target_level", "Head": "at_target_level",
    "Lead": "at_target_level", "Manager": "at_target_level", "VP": "above_target_level",
    "Coordinator": "below_target_level", "Representative": "below_target_level",
    "Engineer": "at_target_level", "Scientist": "at_target_level", "Executive": "at_target_level",
}

LOCATION_BUCKETS = [
    ("austin_onsite", 0.25), ("austin_hybrid", 0.15), ("austin_unstated", 0.10),
    ("remote_us", 0.25), ("remote_limited", 0.05), ("other_city", 0.15), ("unknown", 0.05),
]
OTHER_CITIES = ["Dallas, TX", "Denver, CO", "New York, NY", "San Francisco, CA", "Chicago, IL"]

PAY_BUCKETS = [("stated_meets", 0.30), ("stated_below", 0.15), ("hourly", 0.05), ("missing", 0.50)]
ROLE_BUCKETS = [("match", 0.40), ("adjacent", 0.30), ("mismatch", 0.30)]
QUAL_BUCKETS = [("meets", 0.45), ("partially_meets", 0.40), ("does_not_meet", 0.15)]
STATUS_BUCKETS = [("OPEN", 0.70), ("UNKNOWN", 0.27), ("CLOSED", 0.03)]

DUTY_SENTENCES = {
    "match": [
        "Own the paid acquisition budget across Meta, Google, TikTok and programmatic channels.",
        "Design and run structured experiments on creative, bidding and landing pages.",
        "Build measurement and attribution reporting that ties spend to pipeline and revenue.",
        "Partner with lifecycle, sales and product marketing on full-funnel growth.",
        "Lead and coach a small team of channel specialists and agency partners.",
        "Forecast spend, CAC and payback and present results to leadership monthly.",
    ],
    "adjacent": [
        "Plan and execute regional field events, trade shows and executive dinners.",
        "Manage the editorial calendar, blog, newsletters and social channels.",
        "Own product launch messaging, sales enablement decks and competitive positioning.",
        "Coordinate agencies and vendors for brand campaigns and creative production.",
        "Maintain the marketing automation platform and lead routing rules.",
    ],
    "mismatch": [
        "Design, build and operate the streaming data platform on Kafka and Spark.",
        "Write production Python and SQL pipelines with strong testing discipline.",
        "Prospect and qualify outbound leads and book meetings for account executives.",
        "Own service reliability, on-call rotations and infrastructure cost.",
        "Model customer behavior with statistical and machine-learning methods.",
    ],
}
REQUIREMENT_SENTENCES = [
    "5+ years of hands-on experience in the area described above.",
    "Comfortable with spreadsheets, dashboards and basic SQL.",
    "Clear written communication and a habit of documenting decisions.",
    "Experience managing external partners and budgets.",
    "Bachelor's degree or equivalent practical experience.",
]
BOILERPLATE = (
    "About the company: a fictional organization used only for offline benchmarks. "
    "Nothing in this text describes a real employer, product, or person. "
)

CONSENT_WORDINGS = [
    "I certify that all application information is accurate.",
    "I consent to the processing of my application data for recruiting purposes.",
    "I agree to the candidate privacy notice and confirm my answers are truthful.",
    "By submitting, I certify that I have never been dismissed for cause.",
]


@dataclass
class Question:
    key: str
    semantic_type: str
    required: bool
    explicit: bool
    """EXPLICIT_ANSWER_REQUIRED type: only a saved answer or user input can answer it."""
    tenant_specific: bool
    """Wording differs per tenant, so a GLOBAL saved answer from another tenant cannot match."""
    deterministic: bool
    """Answerable from identity/resume/verified facts without the user."""


BASE_QUESTIONS: dict[str, list[Question]] = {
    "greenhouse": [
        Question("first_name", "FIRST_NAME", True, False, False, True),
        Question("last_name", "LAST_NAME", True, False, False, True),
        Question("email", "EMAIL", True, False, False, True),
        Question("phone", "PHONE", True, False, False, True),
        Question("resume", "RESUME", True, False, False, True),
        Question("linkedin", "LINKEDIN", False, False, False, True),
        Question("work_authorization", "WORK_AUTHORIZATION", True, True, False, False),
        Question("sponsorship", "SPONSORSHIP", True, True, False, False),
        Question("eeo_gender", "EEO_GENDER", False, True, False, False),
        Question("eeo_veteran", "EEO_VETERAN", False, True, False, False),
        Question("why_us", "CUSTOM_LONG_TEXT", True, False, True, False),
    ],
    "lever": [
        Question("full_name", "FULL_NAME", True, False, False, True),
        Question("email", "EMAIL", True, False, False, True),
        Question("phone", "PHONE", True, False, False, True),
        Question("current_company", "CURRENT_COMPANY", False, False, False, True),
        Question("resume", "RESUME", True, False, False, True),
        Question("work_authorization", "WORK_AUTHORIZATION", True, True, False, False),
        Question("consent", "CONSENT", True, True, True, False),
    ],
    "ashby": [
        Question("full_name", "FULL_NAME", True, False, False, True),
        Question("email", "EMAIL", True, False, False, True),
        Question("resume", "RESUME", True, False, False, True),
        Question("location", "LOCATION", True, False, False, True),
        Question("salary_expectation", "SALARY_EXPECTATION", True, True, False, False),
        Question("attestation", "ATTESTATION", True, True, True, False),
    ],
    "smartrecruiters": [
        Question("first_name", "FIRST_NAME", True, False, False, True),
        Question("last_name", "LAST_NAME", True, False, False, True),
        Question("email", "EMAIL", True, False, False, True),
        Question("resume", "RESUME", True, False, False, True),
        Question("consent", "CONSENT", True, True, True, False),
        Question("years_experience", "YEARS_EXPERIENCE", True, False, False, True),
    ],
    "workday": [
        Question("sign_in", "USER_ACTION", True, True, True, False),
        Question("first_name", "FIRST_NAME", True, False, False, True),
        Question("last_name", "LAST_NAME", True, False, False, True),
        Question("email", "EMAIL", True, False, False, True),
        Question("resume", "RESUME", True, False, False, True),
        Question("work_authorization", "WORK_AUTHORIZATION", True, True, False, False),
        Question("sponsorship", "SPONSORSHIP", True, True, False, False),
        Question("eeo_race", "EEO_RACE", False, True, False, False),
        Question("attestation", "ATTESTATION", True, True, True, False),
        Question("custom_select", "CUSTOM_SELECT", True, False, True, False),
    ],
    "generic": [
        Question("full_name", "FULL_NAME", True, False, False, True),
        Question("email", "EMAIL", True, False, False, True),
        Question("resume", "RESUME", True, False, False, True),
        Question("cover_letter", "COVER_LETTER", False, False, False, False),
        Question("custom_text", "CUSTOM_TEXT", True, False, True, False),
    ],
}

CANDIDATE_EVIDENCE = {
    "candidate_id": "cand_fictional",
    "verified_facts": {
        "current_title": "Senior Paid Media Manager",
        "years_experience.paid_media": 8,
        "annual_paid_budget_owned": "USD 4M across search, social and programmatic",
        "channels": ["Google Ads", "Meta", "TikTok", "programmatic"],
        "measurement": "incrementality testing, MMM inputs, multi-touch attribution",
        "team": "led 3 channel specialists and 2 agencies",
    },
    "experience": [
        {"title": "Senior Paid Media Manager", "start": "2021", "end": None, "current": True},
        {"title": "Performance Marketing Manager", "start": "2018", "end": "2021", "current": False},
    ],
    "education": [{"degree": "BBA", "field_of_study": "Marketing", "graduation": "2016"}],
}


def _pick(rng: random.Random, buckets: list[tuple[str, float]]) -> str:
    r = rng.random()
    acc = 0.0
    for name, p in buckets:
        acc += p
        if r < acc:
            return name
    return buckets[-1][0]


def listing_id_for(source: str, source_listing_id: str) -> str:
    """Same rule as core ``listing_id_for`` for id-keyed observations."""
    key = f"src:{source}:id:{source_listing_id}"
    return "lst_" + hashlib.sha256(key.encode()).hexdigest()[:32]


def employer_job_key(ats_type: str, tenant: str, job_id: str) -> str:
    return f"ats:{ats_type}:{tenant}:{job_id}".lower()


def ats_url(ats_type: str, tenant: str, job_id: str) -> str:
    if ats_type == "greenhouse":
        return f"https://boards.greenhouse.example/{tenant}/jobs/{job_id}"
    if ats_type == "lever":
        return f"https://jobs.lever.example/{tenant}/{job_id}"
    if ats_type == "ashby":
        return f"https://jobs.ashby.example/{tenant}/{job_id}"
    if ats_type == "smartrecruiters":
        return f"https://jobs.smartrecruiters.example/{tenant}/{job_id}"
    if ats_type == "workday":
        return f"https://{tenant}.wd5.myworkdayjobs.example/careers/job/Austin/{job_id}"
    return f"https://careers.{tenant}.example/apply/{job_id}"


def posting_url(source: str, source_listing_id: str) -> str:
    return {
        "linkedin": f"https://www.linkedin.example/jobs/view/{source_listing_id}/",
        "builtin": f"https://builtin.example/job/{source_listing_id}",
        "indeed": f"https://www.indeed.example/viewjob?jk={source_listing_id}",
        "google": f"https://www.google.example/search?ibp=htl;jobs&htidocid={source_listing_id}",
    }[source]


def search_url(source: str) -> str:
    return {
        "linkedin": "https://www.linkedin.example/jobs/search/?keywords=paid+media+manager&location=Austin",
        "builtin": "https://builtin.example/jobs/hybrid/office?search=paid+media+manager&city=Austin",
        "indeed": "https://www.indeed.example/jobs?q=paid+media+manager&l=Austin%2C+TX",
        "google": "https://www.google.example/search?q=paid+media+manager+jobs+in+Austin%2C+TX&udm=8",
    }[source]


def description_for(rng: random.Random, role: str, chars: int) -> str:
    sentences = [BOILERPLATE, "Responsibilities:"]
    duties = list(DUTY_SENTENCES[role])
    rng.shuffle(duties)
    sentences += duties
    sentences.append("Requirements:")
    reqs = list(REQUIREMENT_SENTENCES)
    rng.shuffle(reqs)
    sentences += reqs
    text = " ".join(sentences)
    while len(text) < chars:
        text += " " + rng.choice(duties) + " " + rng.choice(reqs)
    return text[:chars]


@dataclass
class Truth:
    role: str
    seniority: str
    quals: str
    location_bucket: str
    pay_bucket: str
    ats_type: str
    tenant: str
    employer_job_id: str
    duplicate_of: str | None = None
    """Listing id of the direct-source record this aggregator record duplicates."""


@dataclass
class Fixture:
    listing: dict[str, Any]
    truth: Truth

    @property
    def id(self) -> str:
        return str(self.listing["id"])


def _location(rng: random.Random, bucket: str) -> tuple[str | None, str, str | None]:
    if bucket == "austin_onsite":
        return "Austin, TX", "ONSITE", None
    if bucket == "austin_hybrid":
        return "Austin, TX", "HYBRID", None
    if bucket == "austin_unstated":
        return "Austin, TX", "UNKNOWN", None
    if bucket == "remote_us":
        return "United States", "REMOTE", "United States"
    if bucket == "remote_limited":
        return "Texas", "REMOTE", "Texas"
    if bucket == "other_city":
        return rng.choice(OTHER_CITIES), rng.choice(["ONSITE", "HYBRID"]), None
    return None, "UNKNOWN", None


def _compensation(rng: random.Random, bucket: str) -> dict[str, Any] | None:
    if bucket == "stated_meets":
        low = rng.choice([100_000, 110_000, 120_000, 135_000, 150_000])
        high = low + rng.choice([20_000, 30_000, 40_000])
        return {"raw_text": f"${low:,} - ${high:,}/yr", "minimum": float(low), "maximum": float(high),
                "currency": "USD", "period": "YEAR"}
    if bucket == "stated_below":
        low = rng.choice([60_000, 70_000, 80_000])
        high = low + rng.choice([10_000, 15_000])
        return {"raw_text": f"${low:,} - ${high:,}/yr", "minimum": float(low), "maximum": float(high),
                "currency": "USD", "period": "YEAR"}
    if bucket == "hourly":
        rate = rng.choice([45, 55, 65])
        return {"raw_text": f"${rate}/hr", "minimum": float(rate), "maximum": None,
                "currency": "USD", "period": "HOUR"}
    return None


class Population:
    """A seeded, streaming generator of fictional listings."""

    def __init__(self, assumptions: Assumptions, seed: int = 42) -> None:
        self.a = assumptions
        self.rng = random.Random(seed)
        self.tenants: dict[str, tuple[str, str]] = {}
        self._n = 0

    def _tenant(self, company: str) -> tuple[str, str]:
        if company not in self.tenants:
            slug = company.lower().replace(" ", "-")
            self.tenants[company] = (self.rng.choice(ATS_TYPES), slug)
        return self.tenants[company]

    def one(self, *, source: str | None = None, enriched: bool | None = None) -> Fixture:
        a, rng = self.a, self.rng
        self._n += 1
        if source is None:
            r = rng.random()
            acc = 0.0
            source = a.sources[-1].name
            for s in a.sources:
                acc += s.share_of_observations
                if r < acc:
                    source = s.name
                    break
        profile = a.source(source)
        if enriched is None:
            enriched = rng.random() < profile.detail_limit / a.max_results_per_source
        role = _pick(rng, ROLE_BUCKETS)
        title = rng.choice(TITLES[role])
        seniority = next((v for k, v in SENIORITY_BY_TITLE_HINT.items() if k in title), "at_target_level")
        quals = _pick(rng, QUAL_BUCKETS) if role == "match" else rng.choice(["partially_meets", "does_not_meet"])
        company = rng.choice(COMPANIES)
        ats_type, tenant = self._tenant(company)
        job_id = str(700_000 + self._n)
        loc_bucket = _pick(rng, LOCATION_BUCKETS)
        location, arrangement, remote_region = _location(rng, loc_bucket)
        pay_bucket = _pick(rng, PAY_BUCKETS)
        status = _pick(rng, STATUS_BUCKETS)
        completeness = profile.detail_completeness if enriched else profile.card_completeness
        if completeness == "FULL":
            description: str | None = description_for(rng, role, rng.randint(2_500, 6_000))
        elif completeness == "PARTIAL":
            description = description_for(rng, role, 300)
        else:
            description = None
        source_listing_id = f"{self._n:07d}"
        apply_link = None
        key = None
        if enriched and rng.random() < profile.p_apply_link_on_detail and ats_type != "generic":
            apply_link = ats_url(ats_type, tenant, job_id)
            key = employer_job_key(ats_type, tenant, job_id)
        listing = {
            "id": listing_id_for(source, source_listing_id),
            "source": source,
            "source_listing_id": source_listing_id,
            "posting_url": posting_url(source, source_listing_id),
            "source_url": posting_url(source, source_listing_id) if enriched else search_url(source),
            "application_url": apply_link,
            "title": title,
            "company": company,
            "location": location,
            "work_arrangement": arrangement,
            "remote_eligibility": remote_region,
            "compensation": _compensation(rng, pay_bucket),
            "description": description,
            "description_completeness": completeness,
            "status": status,
            "posted_text": f"{rng.randint(1, 20)} days ago",
            "observed_at": OBSERVED_AT,
            "evidence": f"fictional {source} {'detail page' if enriched else 'search card'}",
            "provenance": [{
                "source": source,
                "source_listing_id": source_listing_id,
                "posting_url": posting_url(source, source_listing_id),
                "source_url": posting_url(source, source_listing_id) if enriched else search_url(source),
                "employer_job_key": key,
                "application_url": apply_link,
                "observed_at": OBSERVED_AT,
                "evidence": f"fictional {source} observation; job id from the fixture",
                "query_id": "qry_fictional",
            }],
        }
        truth = Truth(role=role, seniority=seniority, quals=quals, location_bucket=loc_bucket,
                      pay_bucket=pay_bucket, ats_type=ats_type, tenant=tenant,
                      employer_job_id=job_id)
        return Fixture(listing, truth)

    def aggregator_duplicate(self, of: Fixture) -> Fixture:
        """A Google observation of the same employer job, sharing its employer key."""
        self._n += 1
        doc_id = f"g{self._n:07d}"
        key = employer_job_key(of.truth.ats_type, of.truth.tenant, of.truth.employer_job_id)
        link = ats_url(of.truth.ats_type, of.truth.tenant, of.truth.employer_job_id)
        listing = dict(of.listing)
        listing.update({
            "id": listing_id_for("google", doc_id),
            "source": "google",
            "source_listing_id": doc_id,
            "posting_url": posting_url("google", doc_id),
            "source_url": search_url("google"),
            "application_url": link,
            "description": description_for(self.rng, of.truth.role, 300),
            "description_completeness": "PARTIAL",
            "evidence": "fictional Google Jobs result and detail pane",
            "provenance": [{
                "source": "google",
                "source_listing_id": doc_id,
                "posting_url": posting_url("google", doc_id),
                "source_url": search_url("google"),
                "employer_job_key": key,
                "application_url": link,
                "observed_at": OBSERVED_AT,
                "evidence": "fictional Google apply option pointing at the employer ATS posting",
                "query_id": "qry_fictional",
            }],
        })
        truth = Truth(**{**of.truth.__dict__, "duplicate_of": of.id})
        return Fixture(listing, truth)

    def stream(self, n: int, *, p_aggregator_dup: float = 0.15) -> Iterator[Fixture]:
        made = 0
        while made < n:
            fx = self.one()
            yield fx
            made += 1
            if made < n and fx.listing["application_url"] and self.rng.random() < p_aggregator_dup:
                yield self.aggregator_duplicate(fx)
                made += 1

    def questions_for(self, tenant: str, ats_type: str) -> list[Question]:
        return list(BASE_QUESTIONS[ats_type])


def generate(assumptions: Assumptions, n: int, seed: int = 42) -> list[Fixture]:
    return list(Population(assumptions, seed).stream(n))


def fixture_json(fixtures: list[Fixture]) -> str:
    return json.dumps({
        "note": "Fictional listings for offline benchmarks. No real employer, posting or person.",
        "listings": [f.listing for f in fixtures],
        "truth": {f.id: f.truth.__dict__ for f in fixtures},
    }, indent=1, sort_keys=True)


def question_catalog_json() -> str:
    return json.dumps({
        "note": "Fictional per-ATS question sets used to model human input. Wording is invented.",
        "consent_wordings": CONSENT_WORDINGS,
        "questions": {ats: [q.__dict__ for q in qs] for ats, qs in BASE_QUESTIONS.items()},
    }, indent=1, sort_keys=True)


def candidate_evidence() -> dict[str, Any]:
    return json.loads(json.dumps(CANDIDATE_EVIDENCE))


__all__ = [
    "CANDIDATE_EVIDENCE", "BASE_QUESTIONS", "Fixture", "Population", "Question", "Truth",
    "candidate_evidence", "fixture_json", "generate", "listing_id_for", "question_catalog_json",
]
