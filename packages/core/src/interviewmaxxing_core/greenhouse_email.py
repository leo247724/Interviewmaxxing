"""Pure, conservative matching of an agent-fetched Greenhouse verification email.

Callers supply provider-normalized messages; this module never reads a mailbox,
opens a browser, or logs a verification code. Sender matching is a routing check,
not an assertion that an arbitrary email payload has been authenticated.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import getaddresses
from typing import Any
from urllib.parse import parse_qs, urlsplit

GREENHOUSE_SENDER = "no-reply@us.greenhouse-mail.io"
# EU-hosted boards (job-boards.eu.greenhouse.io) send the same code mail from the EU sender (seen 2026-10-02, Threecolts)
GREENHOUSE_SENDERS = (GREENHOUSE_SENDER, "no-reply@eu.greenhouse-mail.io")
MAX_AGE_SECONDS = 600
TIMESTAMP_TOLERANCE_SECONDS = 2
_CODE = re.compile(
    r"Copy\s+and\s+paste\s+this\s+code\s+into\s+the\s+security\s+code\s+field"
    r"\s+on\s+your\s+application\s*:\s*"
    r"([a-zA-Z0-9](?:[ \t\r\n]*[a-zA-Z0-9]){7})"
    r"(?![a-zA-Z0-9]|[ \t\r\n]+[a-zA-Z0-9](?![a-zA-Z0-9]))",
    re.IGNORECASE,
)


_GREENHOUSE_JOB_HOSTS = frozenset({
    "job-boards.greenhouse.io", "boards.greenhouse.io",
    "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io",
})
_BOARD = re.compile(r"[A-Za-z0-9_-]+")


def greenhouse_job_identity(url: str) -> tuple[str, str] | None:
    """Return a conservative (board, job_id) identity for a hosted/embed job.

    Tracking parameters do not affect identity. Recognized query identities
    (for/token and gh_jid) must be unambiguous and agree with the hosted path;
    malformed or conflicting identity parameters never become tracking noise.
    """
    if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f]", url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _GREENHOUSE_JOB_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443)):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=200)
    except ValueError:
        return None
    hosted = re.fullmatch(r"/([A-Za-z0-9_-]+)/jobs/([0-9]+)(?:/confirmation)?/?", parsed.path)
    embed = re.fullmatch(r"/embed/job_app(?:/confirmation)?/?", parsed.path) is not None
    if not hosted and not embed:
        return None
    identity = (hosted[1], hosted[2]) if hosted else None
    if embed or "for" in query or "token" in query:
        boards, tokens = query.get("for", []), query.get("token", [])
        if (len(boards) != 1 or len(tokens) != 1 or not _BOARD.fullmatch(boards[0])
                or not re.fullmatch(r"[0-9]+", tokens[0])):
            return None
        query_identity = (boards[0], tokens[0])
        if identity is not None and identity != query_identity:
            return None
        identity = query_identity
    if "gh_jid" in query:
        ids = query["gh_jid"]
        if len(ids) != 1 or identity is None or ids[0] != identity[1]:
            return None
    return identity


class EmailMatchError(ValueError):
    """No unambiguous, fresh email can be bound to the pending challenge."""


@dataclass(frozen=True)
class MatchedCode:
    email_id: str
    code: str = field(repr=False)
    received_at: float


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


def _timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid timestamp")
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timestamp must include timezone")
        result = parsed.timestamp()
    elif isinstance(value, (float, int)):
        result = float(value)
    else:
        raise ValueError("Invalid timestamp")
    if not math.isfinite(result):
        raise ValueError("Invalid timestamp")
    return result


def _addresses(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        return []
    return [address.casefold() for _, address in getaddresses(values) if address]


def choose_code(pending: dict[str, Any], messages: list[dict[str, Any]], now: float) -> MatchedCode:
    """Return exactly one matching message, or raise a code-free ``EmailMatchError``.

    Distinct message IDs are ambiguous even when their codes agree: a resend may
    have invalidated an earlier challenge. Exact duplicate provider results are
    harmless. The caller must separately bind the pending challenge to its tab.
    """
    email = pending.get("email")
    company = pending.get("company")
    if not isinstance(email, str) or _addresses(email) != [email.casefold()]:
        raise EmailMatchError("Pending challenge requires an exact recipient address")
    if not isinstance(company, str) or not company.strip():
        raise EmailMatchError("Pending challenge requires a company")
    try:
        current = _timestamp(now)
        requested = _timestamp(pending.get("code_requested_at"))
    except ValueError as exc:
        raise EmailMatchError("Pending challenge has an invalid timestamp") from exc
    if requested > current or current - requested > MAX_AGE_SECONDS:
        raise EmailMatchError("Pending challenge is stale or future-dated")

    expected_subject = _normalized(f"Security code for your application to {company}")
    matches: set[MatchedCode] = set()
    for message in messages:
        if _addresses(message.get("from")) not in ([s] for s in GREENHOUSE_SENDERS):
            continue
        if email.casefold() not in _addresses(message.get("to")):
            continue
        subject = message.get("subject")
        if not isinstance(subject, str) or _normalized(subject) != expected_subject:
            continue
        try:
            received = _timestamp(message.get("received_at"))
        except ValueError:
            continue
        if (
            received < requested - TIMESTAMP_TOLERANCE_SECONDS
            or received > current
            or current - received > MAX_AGE_SECONDS
        ):
            continue
        email_id, body = message.get("id"), message.get("body")
        if not isinstance(email_id, str) or not email_id.strip() or not isinstance(body, str):
            raise EmailMatchError("Matching email is missing its ID or plain-text body")
        codes = {re.sub(r"\s", "", value) for value in _CODE.findall(body)}
        if len(codes) != 1:
            raise EmailMatchError("Matching email does not contain one unambiguous code")
        matches.add(MatchedCode(email_id, codes.pop(), received))
    if not matches:
        raise EmailMatchError("No fresh matching Greenhouse verification email found")
    if len(matches) != 1:
        raise EmailMatchError("Multiple matching verification emails; challenge is ambiguous")
    return matches.pop()
