"""Conservative normalization of user-supplied application URLs.

The normalized URL is only a lookup alias for duplicate detection; navigation always
uses the URL exactly as the user supplied it. Normalization must never merge two
different jobs, so it only removes what is known to be irrelevant:

* scheme and host are lower-cased; default ports and one trailing path slash dropped;
* well-known pure tracking parameters (``utm_*``, ``gclid``, ``gh_src`` ...) dropped;
* all other query parameters are preserved, with their values, and sorted by name
  (repeated names keep their relative order);
* a fragment is kept only if it looks like a client-side route (``#/...``, ``#!/...``).

Two URLs that normalize differently may still be the same job; that is resolved by
binding an observed ATS job identity (``ApplicationStore.bind_job_identity``), never
by following HTTP redirects.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "igshid",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "gh_src",
        "lever-source",
        "lever-source[]",
        "lever-origin",
    }
)
TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)

_DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidApplicationUrl(ValueError):
    pass


def is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def normalize_application_url(url: str) -> str:
    """Return the duplicate-detection alias for an http(s) application URL."""
    raw = url.strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidApplicationUrl(f"expected an http(s) URL, got {url!r}")
    if not parts.hostname:
        raise InvalidApplicationUrl(f"URL has no host: {url!r}")
    if parts.username or parts.password:
        raise InvalidApplicationUrl("URLs with embedded credentials are not accepted")

    host = parts.hostname.lower().rstrip(".")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidApplicationUrl(str(exc)) from exc
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    kept = sorted(
        ((k, v) for k, v in params if not is_tracking_param(k)), key=lambda kv: kv[0]
    )
    query = urlencode(kept, doseq=False)

    fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""
    return urlunsplit((scheme, netloc, path, query, fragment))
