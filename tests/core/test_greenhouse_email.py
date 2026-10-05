from datetime import UTC, datetime

import pytest

from interviewmaxxing_core.greenhouse_email import (
    EmailMatchError,
    choose_code,
    greenhouse_job_identity,
)

NOW = 1_800_000_000.0
PHRASE = "Copy and paste this code into the security code field on your application: "


@pytest.fixture
def pending():
    return {
        "email": "leo.obrien18@gmail.com",
        "company": "Example Company",
        "code_requested_at": NOW - 30,
        "challenge_id": "challenge-one",
    }


@pytest.fixture
def message():
    return {
        "id": "message-one",
        "from": "Greenhouse <no-reply@us.greenhouse-mail.io>",
        "to": ["Leo <leo.obrien18@gmail.com>"],
        "subject": "Security code for your application to Example Company",
        "received_at": NOW - 20,
        "body": PHRASE + "abCD1234\nThis code expires soon.",
    }


def test_matches_exact_email_and_redacts_repr(pending, message):
    result = choose_code(pending, [message], NOW)
    assert result.email_id == "message-one"
    assert result.code == "abCD1234"
    assert result.received_at == NOW - 20
    assert result.code not in repr(result)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("from", "no-reply@us.greenhouse-mail.io <attacker@example.com>"),
        ("from", "no-reply@us.greenhouse-mail.io.evil.example"),
        ("from", "no-reply@us.greenhouse-mail.io, attacker@example.com"),
        ("to", ["wrong@gmail.com"]),
        ("to", ["leo.obrien18+other@gmail.com"]),
        ("subject", "Security code for your application to Example Company Labs"),
        ("subject", "Re: Security code for your application to Example Company"),
        ("received_at", NOW - 33),
        ("received_at", NOW + 1),
        ("received_at", float("nan")),
        ("received_at", "2026-09-28T12:00:00"),
    ],
)
def test_ignores_nonmatching_or_untrustworthy_metadata(pending, message, key, value):
    message[key] = value
    with pytest.raises(EmailMatchError, match="No fresh matching"):
        choose_code(pending, [message], NOW)


def test_iso_timestamp_and_subject_whitespace(pending, message):
    message["received_at"] = datetime.fromtimestamp(NOW - 20, UTC).isoformat()
    message["subject"] = " SECURITY code for your application to Example   COMPANY "
    assert choose_code(pending, [message], NOW).code == "abCD1234"


def test_timestamp_precision_tolerance(pending, message):
    message["received_at"] = pending["code_requested_at"] - 2
    assert choose_code(pending, [message], NOW).email_id == "message-one"


@pytest.mark.parametrize("requested", [NOW - 601, NOW + 1, float("inf"), None])
def test_rejects_expired_or_invalid_pending(pending, message, requested):
    pending["code_requested_at"] = requested
    with pytest.raises(EmailMatchError):
        choose_code(pending, [message], NOW)


def test_old_email_rejected_even_with_precision_tolerance(pending, message):
    pending["code_requested_at"] = NOW - 600
    message["received_at"] = NOW - 601
    with pytest.raises(EmailMatchError, match="No fresh matching"):
        choose_code(pending, [message], NOW)


@pytest.mark.parametrize("code", ["A B C D 1 2 3 4", "ABCD\n1234", "ABCD1234"])
def test_code_layouts(pending, message, code):
    message["body"] = PHRASE + code
    assert choose_code(pending, [message], NOW).code == "ABCD1234"


@pytest.mark.parametrize(
    "body",
    ["ABCD1234", PHRASE + "ABC123", PHRASE + "ABCDE1234", PHRASE + "A B C D E 1 2 3 4"],
)
def test_does_not_guess_code(pending, message, body):
    message["body"] = body
    with pytest.raises(EmailMatchError, match="one unambiguous code"):
        choose_code(pending, [message], NOW)


def test_rejects_multiple_codes_in_email(pending, message):
    message["body"] += "\n" + PHRASE + "ZZZZ1234"
    with pytest.raises(EmailMatchError, match="one unambiguous code") as caught:
        choose_code(pending, [message], NOW)
    assert "ZZZZ1234" not in str(caught.value)


@pytest.mark.parametrize("code", ["abCD1234", "ZZZZ1234"])
def test_rejects_distinct_matching_messages_even_same_code(pending, message, code):
    other = {**message, "id": "message-two", "body": PHRASE + code}
    with pytest.raises(EmailMatchError, match="Multiple matching"):
        choose_code(pending, [message, other], NOW)


def test_duplicate_provider_result_is_harmless(pending, message):
    assert choose_code(pending, [message, dict(message)], NOW).email_id == message["id"]


def test_conflicting_duplicate_is_rejected(pending, message):
    other = {**message, "body": PHRASE + "ZZZZ1234"}
    with pytest.raises(EmailMatchError, match="Multiple matching"):
        choose_code(pending, [message, other], NOW)


def test_requires_message_id(pending, message):
    del message["id"]
    with pytest.raises(EmailMatchError, match="missing its ID"):
        choose_code(pending, [message], NOW)


@pytest.mark.parametrize("host", [
    "job-boards.greenhouse.io", "boards.greenhouse.io",
    "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io",
])
@pytest.mark.parametrize("suffix", ["", "/confirmation", "/confirmation/?utm_source=test"])
def test_greenhouse_hosted_job_identity(host, suffix):
    assert greenhouse_job_identity(f"https://{host}/example-board/jobs/12345{suffix}") == ("example-board", "12345")


@pytest.mark.parametrize("host", ["boards.greenhouse.io", "job-boards.eu.greenhouse.io"])
@pytest.mark.parametrize("suffix", ["", "/confirmation", "/confirmation/"])
def test_greenhouse_embed_identity_equals_hosted(host, suffix):
    assert greenhouse_job_identity(f"https://{host}/embed/job_app{suffix}?for=example&token=123&utm_source=a") == ("example", "123")


@pytest.mark.parametrize("query", [
    "", "for=example", "token=123", "for=&token=123", "for=example&token=",
    "for=example&token=abc", "for=example&token=123&token=123",
    "for=example&for=other&token=123", "for=example&token=123&for=example",
    "for=other%2Fexample&token=123", "for=example&token=-123",
    "for=example&token=123&gh_jid=456", "for=example&token=123&gh_jid=123&gh_jid=123",
])
def test_greenhouse_embed_rejects_missing_ambiguous_identity(query):
    assert greenhouse_job_identity("https://boards.greenhouse.io/embed/job_app?" + query) is None


@pytest.mark.parametrize("query", [
    "for=other&token=123", "for=example&token=456", "for=example",
    "token=123", "for=example&token=123&token=456", "gh_jid=456",
    "gh_jid=123&gh_jid=123", "gh_jid=",
])
def test_greenhouse_hosted_query_identity_cannot_be_ignored(query):
    assert greenhouse_job_identity("https://boards.greenhouse.io/example/jobs/123?" + query) is None


def test_greenhouse_consistent_query_identity_and_tracking():
    assert greenhouse_job_identity("https://boards.greenhouse.io/example/jobs/123?for=example&token=123&gh_jid=123&source=a&source=b") == ("example", "123")


@pytest.mark.parametrize("url", [
    "http://boards.greenhouse.io/example/jobs/123",
    "https://boards.greenhouse.io.evil.example/example/jobs/123",
    "https://evil.example/example/jobs/123",
    "https://user@boards.greenhouse.io/example/jobs/123",
    "https://boards.greenhouse.io:8443/example/jobs/123",
    "https://boards.greenhouse.io/example/jobs/notnumeric",
    "https://boards.greenhouse.io/example/jobs/123/other",
    "https://boards.greenhouse.io/example/jobs/\uff11\uff12\uff13",
    " https://boards.greenhouse.io/example/jobs/123",
    "https://boards.greenhouse.io/exam\tple/jobs/123",
    "https://[invalid/example/jobs/123", "", None,
])
def test_greenhouse_identity_rejects_noncanonical_or_untrusted_urls(url):
    assert greenhouse_job_identity(url) is None
