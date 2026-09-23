"""Marketing identities over the committed backend's localhost ATS fixture.

Only job definitions and posting copy change. Its forms, validation, upload storage,
submission records and uncertain-outcome behavior are the original mock ATS code.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path


def start_marketing_ats(backend_root: Path, state_dir: Path):
    spec = importlib.util.spec_from_file_location(
        "imx_frontend_fictional_ats", backend_root / "scripts" / "mock_ats.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The committed backend must include its mock ATS fixture.")
    mock = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mock
    spec.loader.exec_module(mock)
    mock.COMPANY = "Fictional Austin Marketing"
    mock.REFERENCE_PREFIX = "FAM"
    skills = replace(mock.SKILLS, options=mock._options(
        ("sk_search", "Paid search"), ("sk_social", "Paid social"),
        ("sk_cro", "Conversion optimization"), ("sk_experiments", "Experiment design"),
    ))
    why = replace(mock.WHY_BRAMBLEWAY, label=f"Why do you want to work at {mock.COMPANY}?")
    certification = replace(
        mock.FAA_CERTIFICATE, name="google_ads_certification",
        label="Do you hold an active Google Ads Search certification?",
    )
    mock.JOBS = {
        "standard": replace(
            mock.JOBS["standard"], code="FAM-MKT-101", title="Paid Acquisition Lead",
            department="Growth Marketing", location="Austin, TX (Hybrid)",
            steps=mock._single(*mock.CORE_FIELDS, mock.YEARS_EXPERIENCE, skills, why),
        ),
        "missing-required": replace(
            mock.JOBS["missing-required"], code="FAM-MKT-102", title="Performance Marketing Manager",
            department="Growth Marketing", location="Remote (US)",
            steps=mock._single(*mock.CORE_FIELDS, mock.NOTICE_PERIOD, mock.SALARY_EXPECTATION, certification),
        ),
        "uncertain": replace(
            mock.JOBS["uncertain"], code="FAM-MKT-103", title="Senior Paid Media Manager",
            department="Growth Marketing", location="Austin, TX (Hybrid)",
        ),
    }

    class MarketingHandler(mock.Handler):
        def get_job(self, slug: str) -> None:
            job = self._job(slug)
            description = "Own paid acquisition, paid search, paid social and pipeline measurement. Lead experiments and budget allocation."
            identity = {
                "@context": "https://schema.org", "@type": "JobPosting",
                "title": job.title,
                "identifier": {"@type": "PropertyValue", "name": mock.COMPANY, "value": job.code},
                "hiringOrganization": {"@type": "Organization", "name": mock.COMPANY},
                "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": job.location}},
                "employmentType": "FULL_TIME", "datePosted": "2026-09-01", "description": description,
            }
            head = (
                f'<link rel="canonical" href="{self.app.origin}/jobs/{job.slug}">'
                '<script type="application/ld+json">' + json.dumps(identity) + "</script>"
            )
            body = (
                mock._job_heading(job) + f"<h2>About the role</h2><p>{description}</p>"
                f'<p><a class="button" href="/jobs/{job.slug}/apply">Apply for this job</a></p>'
                f'<p><a href="/jobs/{job.slug}/application-status">Check your application status</a></p>'
            )
            self._send_html(HTTPStatus.OK, mock.page(job.title, body, head))

    mock.Handler = MarketingHandler
    ats = mock.MockATS(state_dir=state_dir).start()
    return ats, {
        slug: {field: job.describe()[field] for field in ("job_code", "title", "company", "posting_path")}
        for slug, job in mock.JOBS.items()
    }
