import json
import socket
import time

import httpx
import pytest

from interviewmaxxing_service.interview_scraping import (
    InterviewScraper,
    ScrapeError,
    select_company_pages,
    validate_public_url,
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-private-key")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.com", "http://localhost", "http://service.internal", "https://example.com:9000", "https://example.com\\@evil.com", "https://example.com/\nsecret"])
def test_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_rejects_mixed_private_dns_and_normalizes_public_url(monkeypatch):
    assert validate_public_url("https://EXAMPLE.com/#fragment") == "https://example.com/"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError):
        validate_public_url("https://example.com")


def test_selects_relevant_diverse_company_pages_only():
    links = ["/privacy", "/about", "/products", "/products/nested", "/customers", "/pricing", "/blog/news", "https://other.com/about", "/login?return=/about"]
    assert select_company_pages("https://example.com/", links) == ["https://example.com/about", "https://example.com/products", "https://example.com/customers", "https://example.com/pricing"]


def test_realistic_firecrawl_contract_preserves_full_job_and_maps_company():
    requests = []
    full_text = "Responsibilities: own growth. Qualifications: paid marketing experience. Benefits: team and remote work.\n" * 30
    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        assert request.headers["authorization"] == "Bearer test-private-key"
        if request.url.path.endswith("/map"):
            return httpx.Response(200, json={"success": True, "links": [{"url": "https://company.com/about"}, {"url": "https://company.com/products"}]})
        return httpx.Response(200, json={"success": True, "data": {"markdown": full_text, "metadata": {"url": body["url"], "title": "Company", "statusCode": 200}, "links": []}})
    scraper = InterviewScraper(transport=httpx.MockTransport(respond))
    job = scraper.scrape_job("https://jobs.example.com/role")
    assert job["text"] == full_text.strip()
    pages = scraper.scrape_company("https://company.com")
    assert len(pages) == 3
    assert {page["url"] for page in pages} == {"https://company.com/", "https://company.com/about", "https://company.com/products"}
    assert requests[0][1]["onlyMainContent"] is True
    assert requests[0][1]["formats"] == ["markdown"]


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_provider_errors_do_not_expose_upstream_body(status):
    scraper = InterviewScraper(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="private upstream token")))
    with pytest.raises(ScrapeError) as exc:
        scraper.scrape_job("https://example.com/job")
    assert "private" not in str(exc.value)


def test_missing_key_and_unreadable_page_fail_honestly(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    with pytest.raises(ScrapeError, match="FIRECRAWL_API_KEY"):
        InterviewScraper().scrape_job("https://example.com/job")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fixture")
    scraper = InterviewScraper(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"success": True, "data": {"markdown": "Login"}})))
    with pytest.raises(ScrapeError, match="too little"):
        scraper.scrape_job("https://example.com/job")


def test_oversized_job_is_not_silently_truncated():
    scraper = InterviewScraper(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"success": True, "data": {"markdown": "x" * 100_001}})))
    with pytest.raises(ScrapeError, match="not silently truncated"):
        scraper.scrape_job("https://example.com/job")


def test_provider_api_ingestion_indexes_firecrawl_responses(tmp_path, monkeypatch):
    from interviewmaxxing_service import interview_providers
    from interviewmaxxing_service.interviews import InterviewApi

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/map"):
            return httpx.Response(200, json={"success": True, "links": [{"url": "https://company.com/products"}]})
        text = ("The product automates enterprise revenue attribution with warehouse data. " if "company.com" in body["url"] else "This role owns qualified opportunity growth and paid acquisition experiments. ") * 4
        return httpx.Response(200, json={"success": True, "data": {"markdown": text, "metadata": {"url": body["url"], "title": "Imported source"}}})
    scraper = InterviewScraper(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(interview_providers, "InterviewScraper", lambda: scraper)
    api = InterviewApi(tmp_path / "interviews.sqlite3", interview_providers.InterviewProviders())
    try:
        created = api.create({"company": "Company", "title": "Growth lead", "companyUrl": "https://company.com", "jobUrl": "https://jobs.example.com/lead"})
        for _ in range(200):
            session = api.get(created["id"])
            if session["sourceIngestion"]["status"] != "processing":
                break
            time.sleep(.01)
        assert session["sourceIngestion"]["status"] == "ready"
        assert "qualified opportunity growth" in session["jobDescription"]
        assert len([d for d in session["documents"] if d["kind"] == "company"]) == 2
        context = api.context(created["id"], "enterprise revenue attribution")
        assert "warehouse data" in context
        assert "https://company.com/products" in context
    finally:
        api.close()
