#!/usr/bin/env python3
"""Bounded real-room synthetic voice acceptance; never represents Leo's performance.

Run against the running local service and named interview-helper worker. Uses
funded ElevenLabs STT/TTS and OpenRouter/Jev. Prints only a sanitized JSON receipt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from array import array

import httpx
from livekit import rtc

from interviewmaxxing_service.interview_providers import DEFAULT_VOICE_ID, load_interview_env


async def run(base: str, origin: str, timeout: float) -> dict:
    load_interview_env()
    room = rtc.Room()
    received = {"frames": 0, "samples": 0, "last": 0.0, "nonSilentFrames": 0}
    consumers: set[asyncio.Task] = set()
    source = None

    async def consume(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                received["frames"] += 1
                received["samples"] += event.frame.samples_per_channel
                samples = array("h", event.frame.data)
                if samples and sum(sample * sample for sample in samples) / len(samples) > 150 ** 2:
                    received["last"] = time.monotonic()
                    received["nonSilentFrames"] += 1
        finally:
            await stream.aclose()

    @room.on("track_subscribed")
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            task = asyncio.create_task(consume(track))
            consumers.add(task)
            task.add_done_callback(consumers.discard)

    async with httpx.AsyncClient(base_url=base, headers={"Origin": origin}, timeout=45) as client:
        async def post(path: str, body: dict) -> dict:
            response = await client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        state = await post("/interviews", {
            "company": "SYNTHETIC VOICE ACCEPTANCE TEST", "title": "Performance Marketing Lead",
            "jobDescription": "Own paid acquisition, causal measurement, budget allocation and business outcomes.",
            "round": 2, "persona": "hiring_manager", "durationMinutes": 5,
            "resumeText": "Synthetic candidate fixture: managed a paid search experiment. This is not Leo's profile.",
            "notes": "Synthetic automated test, not personal practice. Probe incrementality and attribution.",
            "targetScore": 80,
        })
        sid = state["id"]
        prefix = "/interviews/" + sid
        try:
            await post(prefix + "/start", {})
            connection = await post(prefix + "/connect", {})
            await room.connect(connection["url"], connection["token"])
            source = rtc.AudioSource(24000, 1)
            track = rtc.LocalAudioTrack.create_audio_track("synthetic-microphone", source)
            await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            deadline = time.monotonic() + timeout
            while not received["nonSilentFrames"] or time.monotonic() - received["last"] < 2:
                if time.monotonic() > deadline:
                    raise RuntimeError("No completed interviewer audio received before timeout")
                await asyncio.sleep(0.2)
            opening_frames = received["nonSilentFrames"]
            # Real ElevenLabs synthesis is injected into WebRTC as PCM microphone
            # frames; the worker must recognize it through real streaming STT.
            answer = "In this synthetic example I owned a fifty thousand dollar monthly paid search budget. I separated branded and non branded campaigns. The baseline was two hundred qualified leads at two hundred fifty dollars each. I held out two comparable regions for four weeks and measured qualified opportunities rather than platform conversions. Cost per qualified lead fell to two hundred dollars, but the sample was small and I would not claim causality without a longer test."
            async with httpx.AsyncClient(timeout=45) as speech_client:
                response = await speech_client.post(
                    "https://api.elevenlabs.io/v1/text-to-speech/" + os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID),
                    params={"output_format": "pcm_24000"}, headers={"xi-api-key": os.environ["ELEVEN_API_KEY"]},
                    json={"text": answer, "model_id": "eleven_turbo_v2_5"},
                )
                response.raise_for_status()
                pcm = response.content
            chunk_bytes = 960  # 20ms, 24kHz, signed 16-bit mono
            for index in range(0, len(pcm), chunk_bytes):
                chunk = pcm[index:index + chunk_bytes]
                if len(chunk) % 2:
                    chunk += b"\0"
                await source.capture_frame(rtc.AudioFrame(chunk, 24000, 1, len(chunk) // 2))
            for _ in range(100):
                await source.capture_frame(rtc.AudioFrame(bytes(chunk_bytes), 24000, 1, 480))
            await source.wait_for_playout()
            while time.monotonic() < deadline:
                response = await client.get(prefix)
                response.raise_for_status()
                state = response.json()
                turns = state.get("turns", [])
                scored = [turn for turn in turns if turn.get("score") is not None or turn.get("judgment") is not None]
                transcript = " ".join(turn.get("answer", "") for turn in turns).lower()
                complete = "longer test" in transcript and len(transcript.split()) >= int(len(answer.split()) * 0.75)
                settled = len(turns) == 1 and turns[0].get("scoreStatus") == "completed"
                if len(turns) > 1:
                    raise RuntimeError("One continuous synthetic answer was incorrectly split into multiple graded turns")
                if complete and settled and scored and received["nonSilentFrames"] > opening_frames:
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Voice answer, follow-up audio or live judgment not observed before timeout")
            ended = await post(prefix + "/finish", {})
            await asyncio.sleep(2)
            frames_at_stop = received["nonSilentFrames"]
            await asyncio.sleep(1)
            if received["nonSilentFrames"] != frames_at_stop:
                raise RuntimeError("Audio continued after manual finish")
            return {"sessionId": sid, "synthetic": True, "audioFrames": received["frames"],
                    "audioSamples": received["samples"], "nonSilentFrames": received["nonSilentFrames"], "turnCount": len(turns),
                    "scoredTurnCount": len(scored), "status": ended["status"], "audioStopped": True}
        finally:
            await post(prefix + "/finish", {})
            await room.disconnect()
            if source is not None:
                await source.aclose()
            for task in tuple(consumers):
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--origin", default="http://127.0.0.1:4317")
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.base_url, args.origin, args.timeout)), indent=2))
