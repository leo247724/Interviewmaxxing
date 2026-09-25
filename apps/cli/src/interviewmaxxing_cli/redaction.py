"""What the CLI prints of stored records: page addresses without per-session tokens, and
event metadata without the values a person typed or chose unless they ask for them.

Sites put draft and session tokens in the query or fragment of form URLs, and the store
keeps those URLs exactly (a resume needs the exact step). ``events`` and ``status APP
--json`` print every such URL through ``page_address``: scheme, host and path only, the
rule of the service's ``views.page_address`` (the CLI cannot import the service).
Lookup prompts (they quote the typed value and the site's suggestions), answer
candidates, lookup suggestions, chosen lookup labels and routing traces can carry the
person's own values; ``events`` prints them only with ``--verbose``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

HIDDEN = "(hidden; --verbose shows it)"
"""Stands for a value ``events`` prints only with ``--verbose``."""
ROUTING_EVENT = "routing.trace"
"""The runner's route-decision and trace projection event (``runner.ROUTING_EVENT``)."""
SUGGESTION_EVENT = "field.suggestion_chosen"
"""The runner's chosen-lookup-suggestion event (``runner.SUGGESTION_EVENT``)."""
_URL = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_MISSING_PRIVATE = ("prompt", "candidates")
"""``MissingInput`` keys that can quote the person's values: the prompt (a lookup's typed
value and suggestions, a rejected answer's site message) and an ambiguous answer's
candidates (from their saved answers)."""


def page_address(url: str) -> str | None:
    """``scheme://host[:port]/path`` of an http(s) page, without the query, the fragment
    and any user name or password; None for anything that is not an http(s) URL."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def redact_urls(text: str) -> str:
    """``text`` with every http(s) URL in it reduced to its ``page_address``."""
    return _URL.sub(lambda m: page_address(m.group(0)) or "<url>", text)


def public_value(value: Any) -> Any:
    """A JSON-like value with every URL inside every string reduced to its page address;
    nothing else changes."""
    if isinstance(value, str):
        return redact_urls(value)
    if isinstance(value, dict):
        return {k: public_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [public_value(v) for v in value]
    return value


def _public_missing(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    hidden = {k: HIDDEN for k in _MISSING_PRIVATE if item.get(k)}
    if str(item.get("control_type") or "").upper() == "TYPEAHEAD" and item.get("options"):
        hidden["options"] = HIDDEN  # a lookup's options are the suggestions for its typed value
    return item | hidden


def public_metadata(event: str, metadata: dict[str, Any], *, verbose: bool = False
                    ) -> dict[str, Any]:
    """Event metadata as ``events`` prints it. URLs are always reduced to page addresses.
    Without ``verbose``, the prompts, answer candidates and lookup suggestions of recorded
    questions, a chosen lookup label with its chooser's decision, and the routing
    projection (route decisions and traces) are replaced by ``HIDDEN``."""
    data: dict[str, Any] = public_value(metadata)
    if verbose:
        return data
    missing = data.get("missing_inputs")
    if isinstance(missing, list):
        data["missing_inputs"] = [_public_missing(m) for m in missing]
    if event == ROUTING_EVENT:
        data |= {k: HIDDEN for k in ("fields", "traces") if data.get(k)}
    if event == SUGGESTION_EVENT:
        data |= {k: HIDDEN for k in ("chosen_label", "decision") if data.get(k)}
    return data


__all__ = [
    "HIDDEN",
    "page_address",
    "public_metadata",
    "public_value",
    "redact_urls",
]
