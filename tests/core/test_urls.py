from __future__ import annotations

import pytest

from interviewmaxxing_core import InvalidApplicationUrl
from interviewmaxxing_core import normalize_application_url as norm


def test_tracking_parameters_are_dropped():
    assert norm(
        "https://boards.greenhouse.io/acme/jobs/123?utm_source=li&utm_medium=x&gclid=1&gh_src=abc"
    ) == "https://boards.greenhouse.io/acme/jobs/123"


@pytest.mark.parametrize(
    "url, param",
    [
        ("https://acme.example/careers?gh_jid=4012345", "gh_jid=4012345"),
        ("https://acme.wd1.myworkdayjobs.example/job?jobId=R-100", "jobId=R-100"),
        ("https://jobs.example/apply?id=9&step=1", "id=9"),
        ("https://jobs.example/apply?source=x&token=a%2Fb", "token=a%2Fb"),
    ],
)
def test_meaningful_parameters_are_preserved(url, param):
    assert param in norm(url)


def test_meaningful_parameters_distinguish_jobs():
    assert norm("https://acme.example/careers?gh_jid=1") != norm(
        "https://acme.example/careers?gh_jid=2"
    )


def test_parameter_order_and_repeats():
    assert norm("https://j.example/a?b=2&a=1") == norm("https://j.example/a?a=1&b=2")
    assert norm("https://j.example/a?t=2&t=1") == "https://j.example/a?t=2&t=1"
    assert norm("https://j.example/a?t=2&t=1") != norm("https://j.example/a?t=1&t=2")
    assert norm("https://j.example/a?flag=") == "https://j.example/a?flag="


def test_scheme_host_port_and_trailing_slash():
    assert norm("HTTPS://Jobs.Example:443/Path/To/Job/") == "https://jobs.example/Path/To/Job"
    assert norm("http://localhost:8123/x") == "http://localhost:8123/x"
    assert norm("https://jobs.example") == "https://jobs.example/"


def test_fragments_kept_only_for_client_routes():
    assert norm("https://jobs.example/app#/jobs/42") == "https://jobs.example/app#/jobs/42"
    assert norm("https://jobs.example/app#!/jobs/42") == "https://jobs.example/app#!/jobs/42"
    assert norm("https://jobs.example/job/42#apply-section") == "https://jobs.example/job/42"


@pytest.mark.parametrize(
    "url", ["ftp://jobs.example/x", "jobs.example/x", "https:///nohost", "https://u:p@j.example/x",
            "javascript:alert(1)"]
)
def test_invalid_urls_rejected(url):
    with pytest.raises(InvalidApplicationUrl):
        norm(url)
