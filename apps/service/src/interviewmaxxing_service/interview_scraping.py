"""Bounded, server-only Firecrawl ingestion for interview context."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_TEXT = 100_000
MAX_RESPONSE = 2_000_000
API = "https://api.firecrawl.dev/v2"


class ScrapeError(RuntimeError):
    """Safe explanation suitable for an ingestion error card."""


def validate_public_url(value: str) -> str:
    """Validate before handing a URL to the remote scraper; never fetch it locally."""
    try:
        if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 33 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in {None, 80, 443} or "\\" in value or "%" in host:
            raise ValueError
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
        if "." not in host or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid")):
            raise ValueError
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise ValueError
        return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError, OSError):
        raise ValueError("Enter a reachable public http or https website URL, without credentials or a custom port.") from None


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.").rstrip(".")


def select_company_pages(home: str, links: list[Any]) -> list[str]:
    """Prioritize business context with category diversity, never a whole-site crawl."""
    groups = [r"about|company|mission|team", r"product|platform|solution|service", r"customer|case-stud|industr|use-case", r"pricing|plan"]
    buckets: list[list[tuple[int, str]]] = [[] for _ in groups]
    seen = {home.rstrip("/")}
    for item in links[:200]:
        raw = item.get("url") if isinstance(item, dict) else item
        if not isinstance(raw, str):
            continue
        url = urljoin(home, raw)
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or _site(url) != _site(home) or parsed.query or parsed.fragment or parsed.username:
            continue
        if parsed.port not in {None, 80, 443}:
            continue
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).rstrip("/")
        path = parsed.path.lower()
        if url in seen or re.search(r"privacy|terms|cookie|login|sign-in|signup|legal|careers|/jobs|/blog|/news|\.pdf$", path):
            continue
        seen.add(url)
        for index, pattern in enumerate(groups):
            if re.search(pattern, path):
                buckets[index].append((len(path), url))
                break
    chosen = []
    leftovers = []
    for bucket in buckets:
        bucket.sort()
        if bucket:
            chosen.append(bucket[0][1])
            leftovers.extend(bucket[1:])
    chosen.extend(url for _, url in sorted(leftovers))
    return chosen[:4]


class InterviewScraper:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def _request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        key = os.getenv("FIRECRAWL_API_KEY")
        if not key:
            raise ScrapeError("Add FIRECRAWL_API_KEY to env.local, restart Interview Helper, then retry website import.")
        try:
            with httpx.Client(timeout=httpx.Timeout(50, connect=10), transport=self.transport, follow_redirects=False) as client, client.stream("POST", API + endpoint, headers={"Authorization": "Bearer " + key}, json=body) as response:
                if response.status_code in {401, 403}:
                    raise ScrapeError("Firecrawl rejected the API key or page access. Check the account and retry website import.")
                if response.status_code == 402:
                    raise ScrapeError("Firecrawl credits are unavailable. Refill the account and retry website import.")
                if response.status_code == 429:
                    raise ScrapeError("Firecrawl is rate limited. Wait briefly and retry website import.")
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise ScrapeError("The website response is too large to import safely. Use a more specific page URL.")
                result = json.loads(content)
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ScrapeError("Firecrawl could not read this page. Check that the public URL opens and retry website import.")
            return result
        except (httpx.HTTPError, ValueError, TypeError):
            raise ScrapeError("Website import failed or timed out. Check the public URL and retry website import.") from None

    def scrape_job(self, url: str) -> dict[str, str]:
        return self._scrape(validate_public_url(url))

    def _scrape(self, url: str, *, include_links: bool = False) -> dict[str, Any]:
        data = self._request("/scrape", {"url": url, "formats": ["markdown", "links"] if include_links else ["markdown"],
                                        "onlyMainContent": True, "timeout": 40000}).get("data")
        if not isinstance(data, dict):
            raise ScrapeError("Firecrawl returned no readable website content. Retry website import.")
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        status = metadata.get("statusCode", 200)
        text = data.get("markdown")
        if not isinstance(status, int) or status >= 400 or metadata.get("error"):
            raise ScrapeError("The website blocked access or returned an error page. Check the URL and retry website import.")
        if not isinstance(text, str) or len(text.strip()) < 80:
            raise ScrapeError("The page contained too little readable text. Use the full public job listing or company page URL.")
        if len(text) > MAX_TEXT:
            raise ScrapeError("The page exceeds 100,000 characters. Use a more specific page URL; text was not silently truncated.")
        final_url = validate_public_url(metadata.get("url") or metadata.get("sourceURL") or url)
        return {"text": text.strip(), "title": str(metadata.get("title") or urlsplit(final_url).hostname)[:200],
                "url": final_url, **({"links": data.get("links", [])} if include_links else {})}

    def scrape_company(self, url: str) -> list[dict[str, str]]:
        url = validate_public_url(url)
        home = self._scrape(url, include_links=True)
        if _site(home["url"]) != _site(url):
            raise ScrapeError("The company URL redirects to another domain. Enter the final company website URL and retry.")
        mapped = self._request("/map", {"url": home["url"], "limit": 100, "includeSubdomains": False})
        links = mapped.get("links", [])
        if not isinstance(links, list):
            raise ScrapeError("Firecrawl returned an invalid website map. Retry website import.")
        home_links = home.pop("links", [])
        targets = select_company_pages(home["url"], links + (home_links if isinstance(home_links, list) else []))
        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = list(pool.map(self.scrape_job, targets))
        if any(_site(page["url"]) != _site(home["url"]) for page in pages):
            raise ScrapeError("A selected company page redirected off-site. Check the website URL before retrying.")
        return [home, *pages]
