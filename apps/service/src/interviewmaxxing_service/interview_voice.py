"""Local LiveKit worker. Run with ``python -m interviewmaxxing_service.interview_voice start``.

Only room metadata (a session ID) crosses the dispatch boundary. Grounding and
judgments stay in the local service store; no transcripts are written to logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from typing import Any

from .errors import ApiError

log = logging.getLogger(__name__)


def session_id_from_metadata(metadata: str) -> str:
    value = json.loads(metadata or "{}")
    sid = value.get("sessionId") if isinstance(value, dict) else None
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        raise ValueError("Voice dispatch requires a sessionId")
    return sid


class VoiceBridge:
    """Bind actual played questions and finalized STT to durable asynchronous grading."""

    def __init__(self, api: Any, session_id: str) -> None:
        self.api = api
        self.session_id = session_id
        self.question = ""
        self.seen: set[str] = set()
        self.pending: set[asyncio.Task[Any]] = set()

    def conversation_item(self, item: Any) -> None:
        if getattr(item, "role", None) not in {"assistant", "user"}:
            return
        item_id = str(item.id)
        text = (item.text_content or "").strip()
        if not text or item_id in self.seen:
            return
        self.seen.add(item_id)
        if item.role == "assistant":
            self.question = text
            try:
                self.api.record_voice_question(self.session_id, text)
            except ApiError as exc:
                # Playback completion can arrive after the finish transaction.
                if exc.code != "conflict" or self.api.get(self.session_id)["status"] == "active":
                    raise
        elif item.role == "user" and self.question:
            # The API persists before awaiting Jev. Never await that provider on
            # the voice event callback or before generating the next question.
            task = asyncio.create_task(asyncio.to_thread(
                self.api.voice_turn, self.session_id, self.question, text, "voice:" + item_id,
            ))
            self.pending.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self.pending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("Voice turn persistence or judgment failed; inspect session status")

    async def drain(self) -> None:
        if self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)


async def close_voice_api(bridge: VoiceBridge) -> None:
    """Retain the last queued judgment after audio stops and on worker shutdown."""
    await bridge.drain()
    await asyncio.to_thread(bridge.api.close)


async def wait_for_candidate(ctx: Any, sid: str, closed: asyncio.Event, active: Any, timeout: float = 60) -> bool:
    """Explicit dispatch can arrive before the browser; never speak into an empty room."""
    participant = asyncio.create_task(ctx.wait_for_participant(identity="candidate-" + sid))
    disconnected = asyncio.create_task(closed.wait())
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not closed.is_set() and active():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            done, _ = await asyncio.wait({participant, disconnected}, timeout=min(0.5, remaining),
                                         return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                return False
            if participant in done:
                participant.result()
                return active() and not closed.is_set()
        return False
    finally:
        for task in (participant, disconnected):
            if not task.done():
                task.cancel()
        await asyncio.gather(participant, disconnected, return_exceptions=True)


async def interview_entrypoint(ctx: Any) -> None:
    # Lazy imports keep lifecycle helpers usable without native audio dependencies.
    from livekit.agents import Agent, AgentSession, room_io
    from livekit.plugins import elevenlabs, openai, silero

    from interviewmaxxing_core import LocalPaths

    from .interview_providers import (
        DEFAULT_VOICE_ID,
        INTERVIEW_MODEL,
        InterviewProviders,
        interviewer_instructions,
        load_interview_env,
    )
    from .interviews import InterviewApi

    load_interview_env()
    sid = session_id_from_metadata(ctx.job.metadata or ctx.room.metadata)
    paths = LocalPaths.from_env()
    api = InterviewApi(paths.state_db.parent / "interviews.sqlite3", InterviewProviders(), candidate_id=paths.candidate_id)
    bridge = VoiceBridge(api, sid)
    ctx.add_shutdown_callback(lambda: close_voice_api(bridge))
    state = api.get(sid)
    if state["status"] != "active":
        ctx.shutdown(reason="Interview is not active")
        return
    bridge.question = state.get("currentQuestion") or ""

    class Interviewer(Agent):
        async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
            context = api.context(sid, new_message.text_content or "")
            await self.update_instructions(interviewer_instructions(context))

    session = AgentSession(
        # Agent.default.stt_node never manually flushes streaming STT. Eleven's
        # server VAD must commit final text; local VAD alone can leave partials.
        stt=elevenlabs.STT(api_key=os.environ["ELEVEN_API_KEY"], model="scribe_v2_realtime", enable_logging=False,
                          server_vad={"vad_silence_threshold_secs": 1.5, "min_silence_duration_ms": 1000}),
        tts=elevenlabs.TTS(api_key=os.environ["ELEVEN_API_KEY"], voice_id=os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID), enable_logging=False),
        llm=openai.LLM(model=os.getenv("IMX_INTERVIEW_MODEL", INTERVIEW_MODEL), api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1", max_completion_tokens=350),
        vad=silero.VAD.load(min_silence_duration=1.0),
        # Provider final chunks can arrive mid-answer. Accumulate them until
        # local audio silence, allowing the final server commit to catch up.
        turn_handling={"turn_detection": "vad", "endpointing": {"min_delay": 2.5, "max_delay": 4.0},
                       "interruption": {"enabled": True}},
    )
    session.on("conversation_item_added", lambda event: bridge.conversation_item(event.item))
    closed = asyncio.Event()
    session.on("close", lambda event: closed.set())
    ctx.room.on("disconnected", lambda *args: closed.set())
    session_started = False
    # A reconnecting browser keeps the same agent and transcript. Disconnect
    # immediately interrupts playback; the server deadline still runs.
    @ctx.room.on("participant_disconnected")
    def participant_disconnected(participant: Any) -> None:
        if session_started and participant.identity == "candidate-" + sid:
            session.interrupt(force=True)

    try:
        await ctx.connect()
        if not await wait_for_candidate(ctx, sid, closed, lambda: api.get(sid)["status"] == "active"):
            return
        await session.start(
            room=ctx.room, agent=Interviewer(instructions=interviewer_instructions(api.context(sid))),
            room_options=room_io.RoomOptions(close_on_disconnect=False, text_input=False),
        )
        session_started = True
        if bridge.question:
            session.say(bridge.question, allow_interruptions=True)
        else:
            session.generate_reply(instructions="Begin the interview now. Ask exactly one concise, demanding question grounded in the provided job and candidate evidence.")
        while not closed.is_set():
            state = api.get(sid)  # get enforces the authoritative server deadline.
            if state["status"] != "active":
                await session.interrupt(force=True)
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(closed.wait(), timeout=0.5)
    finally:
        await session.aclose()
        await close_voice_api(bridge)
        ctx.shutdown(reason="Interview ended")


def build_server() -> Any:
    from livekit.agents import AgentServer

    from .interview_providers import load_interview_env

    load_interview_env()
    server = AgentServer(
        num_idle_processes=0, host="127.0.0.1", port=int(os.getenv("IMX_VOICE_WORKER_PORT", "8767")),
        log_level="WARN", shutdown_process_timeout=240,
    )
    # Spawn-based macOS workers must pickle this module-level function.
    server.rtc_session(agent_name="interview-helper")(interview_entrypoint)
    return server


def main() -> None:
    from livekit.agents import cli
    cli.run_app(build_server())


if __name__ == "__main__":
    main()
