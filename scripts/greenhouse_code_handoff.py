"""Agent-mediated Gmail -> existing personal Chrome Greenhouse code field.

request prints a read-only Gmail tool request. fill reads full Gmail tool results
from stdin, validates them, and fills only the bound challenge. Never submits.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from interviewmaxxing_core.greenhouse_email import (
    EmailMatchError,
    choose_code,
    greenhouse_job_identity,
)

EMAIL = "leo.obrien18@gmail.com"
DEFAULT_PENDING = Path(__file__).resolve().parents[1] / ".imx/dynamic-applications/real-chrome/gh-run/pending-code.json"


class HandoffError(ValueError):
    pass


def write_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def browser(session: str, *args: str) -> Any:
    result = subprocess.run(["opencli", "browser", session, *args], capture_output=True,
                            text=True, timeout=45)
    if result.returncode:
        raise HandoffError("OpenCLI command failed; challenge retained")
    text = result.stdout.lstrip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
    except ValueError as exc:
        raise HandoffError("OpenCLI did not return structured evidence") from exc
    if isinstance(obj, dict) and obj.get("error"):
        raise HandoffError("OpenCLI returned an error; challenge retained")
    for _ in range(3):
        if isinstance(obj, dict) and "result" in obj:
            obj = obj["result"]
        elif isinstance(obj, dict) and "value" in obj and len(obj) <= 3:
            obj = obj["value"]
        elif isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except ValueError:
                break
        else:
            break
    return obj


# Bind only a single visible code field. Unknown/split-code widgets hold for an
# explicit adapter rather than guessing a field among the rest of the application.
BIND_JS = r'''(() => {
 const a=ARGS;
 const visible=e=>e.getClientRects().length && !e.disabled && !e.readOnly;
 const fields=[...document.querySelectorAll('input')].filter(e=>visible(e) &&
  !/hidden|submit|checkbox|radio|file|password/.test(e.type) &&
  (/security.?code|verification.?code|one.?time.?code/i.test([e.id,e.name,e.autocomplete,e.getAttribute('aria-label'),...Array.from(e.labels||[],l=>l.innerText)].join(' '))));
 if(fields.length!==1) return {ok:false,reason:'expected_one_code_field',count:fields.length};
 const e=fields[0]; e.dataset.imxEmailChallenge=a.challenge;
 window.__imxEmailChallenge={id:a.challenge, url:location.href, field:e};
 return {ok:true,url:location.href,title:document.title};
})()'''

FILL_JS = r'''(() => {
 const a=ARGS, b=window.__imxEmailChallenge;
 if(!b || b.id!==a.challenge || b.url!==location.href || location.href!==a.url)
  return {ok:false,reason:'challenge_or_url_changed'};
 const e=b.field;
 if(!e || !e.isConnected || e.dataset.imxEmailChallenge!==a.challenge || !e.getClientRects().length || e.disabled || e.readOnly)
  return {ok:false,reason:'code_field_changed'};
 if(e.value && e.value!==a.code) return {ok:false,reason:'field_contains_different_code'};
 const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
 e.focus(); setter.call(e,a.code);
 e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));
 return {ok:e.value===a.code,url:location.href,verified:e.value===a.code,submitted:false};
})()'''
VERIFY_JS = r'''(() => {
 const a=ARGS,b=window.__imxEmailChallenge;
 return {ok:!!(b && b.id===a.challenge && b.url===location.href && location.href===a.url && b.field.isConnected && b.field.value===a.code),url:location.href,submitted:false};
})()'''


def create_handoff(entry: dict, session: str, pending_path: Path, *,
                   expected_url: str | None = None, allow_local_fixture: bool = False,
                   rebind: bool = False) -> dict:
    if pending_path.exists() and not rebind:
        raise HandoffError("A challenge is already pending")
    if rebind:
        original = json.loads(pending_path.read_text())
        if original != entry or not entry.get("browser_tab"):
            raise HandoffError("Rebinding requires intact original tab and challenge metadata")
        if entry.get("handoff_state") == "code_filled":
            raise HandoffError("A filled challenge cannot be rebound")
    expected_url = expected_url or entry["url"]
    identity = greenhouse_job_identity(entry.get("url", expected_url))
    if identity and greenhouse_job_identity(expected_url) != identity:
        raise HandoffError("Challenge belongs to a different Greenhouse job")
    tabs = browser(session, "tab", "list")
    matches = [t for t in tabs if t.get("url") == expected_url and (
        not entry.get("browser_tab") or t.get("page") == entry["browser_tab"]
    )]
    if len(matches) != 1 or not matches[0].get("page"):
        raise HandoffError("Cannot identify exactly one original application tab")
    parts = urlsplit(expected_url)
    if parts.scheme != "https" and not (
        allow_local_fixture and parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
    ):
        raise HandoffError("Expected an HTTPS application tab")
    challenge = uuid.uuid4().hex
    tab = matches[0]["page"]
    bound = browser(session, "eval", BIND_JS.replace("ARGS", json.dumps({"challenge": challenge})), "--tab", tab)
    if not isinstance(bound, dict) or not bound.get("ok") or bound.get("url") != expected_url:
        raise HandoffError("Code field could not be bound to original application")
    pending = {**entry, "email": EMAIL, "browser_session": session, "browser_tab": tab,
               "challenge_url": expected_url, "challenge_id": challenge, "handoff_state": "waiting_for_email"}
    write_private(pending_path, pending)
    return request(pending, pending_path)


def request(pending: dict, pending_path: Path = DEFAULT_PENDING) -> dict:
    if not pending.get("challenge_id") or not pending.get("browser_tab"):
        raise HandoffError("Legacy pending state must be rebound to its existing code tab")
    company = pending["company"].replace('"', ' ').replace("\n", " ")
    query = (f'from:{{no-reply@us.greenhouse-mail.io no-reply@eu.greenhouse-mail.io}} to:{EMAIL} '
             f'subject:"Security code for your application to {company}" '
             f'after:{int(pending["code_requested_at"])-2}')
    return {"action": "read_matching_gmail_then_fill", "challenge_id": pending["challenge_id"],
            "company": pending["company"], "email": EMAIL,
            "search_tool": "gmail_search_emails", "search_arguments": {"query": query, "max_results": 20},
            "read_tool": "gmail_read_email", "read_format": "full",
            "fill_command": ["uv", "run", "--no-sync", "python", str(Path(__file__).resolve()),
                             "fill", "--pending", str(pending_path.resolve())],
            "stdin_schema": {"challenge_id": pending["challenge_id"], "messages": ["full Gmail structuredContent objects"]},
            "next": "Pass ALL full matching Gmail structuredContent messages to fill via stdin, with this challenge_id. Do not use snippets or print codes.",
            "browser_session": pending["browser_session"], "browser_tab": pending["browser_tab"]}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_gmail(message: dict) -> dict:
    # Accept a full read_email structuredContent, never a search snippet.
    payload = message.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    texts: list[str] = []
    def walk(part: dict) -> None:
        mime = part.get("mime_type") or part.get("mimeType")
        body = part.get("body") or {}
        text = body.get("content")
        encoded = body.get("base64_url_content") or body.get("data")
        if text is None and encoded:
            text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        if text and mime in {"text/plain", "text/html"}:
            if mime == "text/html":
                parser = _Text()
                parser.feed(text)
                text = " ".join(parser.parts)
            texts.append(text)
        for child in part.get("parts") or []:
            walk(child)
    walk(payload)
    if not texts or not message.get("internal_date"):
        raise HandoffError("A full Gmail message body and internal_date are required")
    return {"id": message["id"], "from": headers.get("from", ""),
            "to": [headers.get("to", "")], "subject": headers.get("subject", ""),
            "received_at": float(message["internal_date"]) / 1000, "body": "\n".join(texts)}


def fill_from_gmail(pending_path: Path, envelope: dict, *, now: float | None = None) -> dict:
    # Prevent two agent continuations racing to fill different emails.
    fd = os.open(str(pending_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pending = json.loads(pending_path.read_text())
        if envelope.get("challenge_id") != pending.get("challenge_id"):
            raise HandoffError("Agent response belongs to another challenge")
        if pending.get("handoff_state") != "waiting_for_email":
            raise HandoffError("Challenge is not waiting for an email")
        messages = [normalize_gmail(m) for m in envelope.get("messages", [])]
        matched = choose_code(pending, messages, now=time.time() if now is None else now)
        args = {"challenge": pending["challenge_id"], "url": pending["challenge_url"], "code": matched.code}
        # Explicit tab ID + in-document binding protect against session reuse or navigation.
        filled = browser(pending["browser_session"], "eval", FILL_JS.replace("ARGS", json.dumps(args)), "--tab", pending["browser_tab"])
        if not isinstance(filled, dict) or not filled.get("ok"):
            raise HandoffError("Bound code field changed; no successful fill verified")
        verified = browser(pending["browser_session"], "eval", VERIFY_JS.replace("ARGS", json.dumps(args)), "--tab", pending["browser_tab"])
        if not isinstance(verified, dict) or not verified.get("ok"):
            raise HandoffError("Code did not survive page readback")
        pending.update(handoff_state="code_filled", gmail_message_id=matched.email_id,
                       code_filled_at=time.time() if now is None else now)
        write_private(pending_path, pending)
        return {"result": "code_filled", "company": pending["company"], "gmail_message_id": matched.email_id,
                "browser_tab": pending["browser_tab"], "verified": True, "submitted": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["request", "fill", "rebind"])
    parser.add_argument("--pending", type=Path, default=DEFAULT_PENDING)
    args = parser.parse_args()
    try:
        if args.command == "request":
            result = request(json.loads(args.pending.read_text()), args.pending)
        elif args.command == "rebind":
            pending = json.loads(args.pending.read_text())
            result = create_handoff(pending, pending["browser_session"], args.pending,
                                    expected_url=pending.get("challenge_url"), rebind=True)
        else:
            result = fill_from_gmail(args.pending, json.load(sys.stdin))
        print(json.dumps(result))
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired) as exc:
        # Never echo tool payloads or code-bearing browser commands.
        reason = str(exc) if isinstance(exc, (HandoffError, EmailMatchError)) else type(exc).__name__
        print(json.dumps({"result": "held", "reason": reason}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
