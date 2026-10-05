"""Voice event persistence is based on actual finalized audio events."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from interviewmaxxing_service.interview_voice import VoiceBridge, session_id_from_metadata


@pytest.mark.parametrize("metadata", ["{}", '[]', '{"sessionId":0}', '{"sessionId":""}'])
def test_invalid_dispatch(metadata):
    with pytest.raises(ValueError):
        session_id_from_metadata(metadata)


def test_dispatch_has_only_required_session_identity():
    assert session_id_from_metadata('{"sessionId":"test-session"}') == "test-session"


def test_actual_questions_pair_with_answers_and_grading_does_not_block():
    async def check():
        gate = threading.Event()
        calls = []

        class Api:
            def record_voice_question(self, sid, text):
                calls.append(("question", text))

            def voice_turn(self, sid, question, answer, request_id):
                calls.append(("answer", question, answer, request_id))
                gate.wait(timeout=2)

        bridge = VoiceBridge(Api(), "session")
        def item(id, role, text):
            return SimpleNamespace(id=id, role=role, text_content=text)
        bridge.conversation_item(item("orphan", "user", "An answer before any actual question"))
        bridge.conversation_item(item("q1", "assistant", "What did you own?"))
        bridge.conversation_item(item("a1", "user", "The budget"))
        bridge.conversation_item(item("a1", "user", "The budget"))
        bridge.conversation_item(item("q2", "assistant", "How much?"))
        await asyncio.sleep(0.03)
        assert calls == [("question", "What did you own?"), ("question", "How much?"),
                         ("answer", "What did you own?", "The budget", "voice:a1")]
        assert len(bridge.pending) == 1
        gate.set()
        await bridge.drain()
    asyncio.run(check())


def test_failed_grading_is_drained_without_stopping_voice(caplog):
    async def check():
        class Api:
            def record_voice_question(self, *args):
                pass

            def voice_turn(self, *args):
                raise RuntimeError("secret upstream response")

        bridge = VoiceBridge(Api(), "session")
        bridge.question = "Why?"
        bridge.conversation_item(SimpleNamespace(id="a", role="user", text_content="Because"))
        await bridge.drain()
        await asyncio.sleep(0)
    asyncio.run(check())
    assert "secret upstream response" not in caplog.text
    assert "inspect session status" in caplog.text


def test_worker_entrypoint_is_spawn_pickleable():
    from multiprocessing.reduction import ForkingPickler

    from interviewmaxxing_service.interview_voice import interview_entrypoint
    assert ForkingPickler.loads(ForkingPickler.dumps(interview_entrypoint)) is interview_entrypoint


def test_last_queued_judgment_drains_after_answer_dispatch():
    from concurrent.futures import ThreadPoolExecutor

    from interviewmaxxing_service.interview_voice import close_voice_api

    async def check():
        judged = threading.Event()

        class Api:
            def __init__(self):
                self.pool = ThreadPoolExecutor(max_workers=1)

            def voice_turn(self, *args):
                self.pool.submit(lambda: judged.set())

            def close(self):
                self.pool.shutdown(wait=True)

        bridge = VoiceBridge(Api(), "session")
        bridge.question = "What was the result?"
        bridge.conversation_item(SimpleNamespace(id="last", role="user", text_content="Twenty percent"))
        await close_voice_api(bridge)
        assert judged.is_set()
    asyncio.run(check())


def test_agent_handoff_is_not_a_transcript_message():
    from livekit.agents.llm import AgentHandoff
    bridge = VoiceBridge(object(), "session")
    bridge.conversation_item(AgentHandoff(old_agent_id=None, new_agent_id="interviewer"))
    assert not bridge.seen
    assert bridge.question == ""


def test_playback_completion_after_finish_is_expected():
    from interviewmaxxing_service.errors import conflict

    class Api:
        def record_voice_question(self, *args):
            raise conflict("This practice has ended.")

        def get(self, sid):
            return {"status": "completed"}

    bridge = VoiceBridge(Api(), "session")
    bridge.conversation_item(SimpleNamespace(id="late", role="assistant", text_content="Final question"))


def test_unexpected_question_conflict_still_surfaces():
    from interviewmaxxing_service.errors import ApiError, conflict

    class Api:
        def record_voice_question(self, *args):
            raise conflict("Some other conflict")

        def get(self, sid):
            return {"status": "active"}

    bridge = VoiceBridge(Api(), "session")
    with pytest.raises(ApiError):
        bridge.conversation_item(SimpleNamespace(id="q", role="assistant", text_content="Question"))


def test_opening_waits_for_exact_candidate_and_room_disconnect_ends_wait():
    from interviewmaxxing_service.interview_voice import wait_for_candidate

    async def check():
        joined = asyncio.Event()
        closed = asyncio.Event()
        identities = []

        class Context:
            async def wait_for_participant(self, *, identity):
                identities.append(identity)
                await joined.wait()
                return object()

        task = asyncio.create_task(wait_for_candidate(Context(), "sid", closed, lambda: True))
        await asyncio.sleep(0.01)
        assert not task.done()
        assert identities == ["candidate-sid"]
        closed.set()
        assert await asyncio.wait_for(task, 0.2) is False
        closed.clear()
        joined.set()
        assert await wait_for_candidate(Context(), "sid", closed, lambda: True) is True
        joined.clear()
        assert await wait_for_candidate(Context(), "sid", closed, lambda: True, timeout=0.01) is False
        assert await wait_for_candidate(Context(), "sid", closed, lambda: False) is False
    asyncio.run(check())
