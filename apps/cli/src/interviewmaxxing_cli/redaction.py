"""What the CLI prints of stored records: page addresses without per-session tokens, and
event metadata without the values a person typed or chose unless they ask for them.

Sites put draft and session tokens in the query or fragment of form URLs, and the store
keeps those URLs exactly (a resume needs the exact step). ``events``, ``status APP
--json`` and ``approve --json`` print every such URL through ``page_address``: scheme,
host and path only, the rule of the service's ``views.page_address`` (the CLI cannot
import the service). Lookup prompts (they quote the typed value and the site's
suggestions), answer candidates, lookup suggestions, chosen lookup labels, the site's
rejection messages and routing traces can carry the person's own values; ``events``
prints them only with ``--verbose``.

``events`` is the history a person may paste into a report or hand to someone else, so
it hides those values by default. ``status APP`` is not: it is the person's own working
view of one application, and it prints what they need to answer it, as ``apply`` and
``resume`` do: each recorded question's prompt, answer candidates and options (a
lookup's suggestions). ``status APP --json`` adds the pending questions as recorded and
the latest packet with its answers (the person's values), with URLs reduced to page
addresses and nothing else hidden. Neither is meant to be kept or shared; ``events``
(without ``--verbose``) is.
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
REJECTION_EVENT = "validation.rejected"
"""The runner's site-rejection event (``runner.REJECTION_EVENT``). Its ``fields[].message``
is the site's validation message, which can quote the typed value; the runner stores it
redacted, but events recorded before that keep the site's text."""
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
    questions, a chosen lookup label with its chooser's decision, the site's messages of
    a rejection (``fields[].message``) and the routing projection (route decisions and
    traces) are replaced by ``HIDDEN``."""
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
    if event == REJECTION_EVENT and isinstance(data.get("fields"), list):
        data["fields"] = [item | {"message": HIDDEN}
                          if isinstance(item, dict) and item.get("message") else item
                          for item in data["fields"]]
    return data


__all__ = [
    "HIDDEN",
    "page_address",
    "public_metadata",
    "public_value",
    "redact_urls",
]
