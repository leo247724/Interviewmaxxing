# Interview Helper acceptance

Run date: 2026-10-02. Status: MVP integrated acceptance passed; personal mock requires Leo. Synthetic practice scores below are tests, not Leo's interview performance.

## Verified evidence

- Real Ashby posting API returned the supplied NeuroScale Founding Performance Marketing Lead job; source snapshot is `neuroscale-job.json`. Prepared draft retains real resume/profile; round 2, hiring manager and 30 minutes are editable assumptions. Actual previous-round material is not supplied.
- Python service regression suite: 241 passed. Frontend unit suite: 201 passed, including the final feedback additions; focused frontend suite 27 passed. Focused interview backend/provider/voice/launcher checks: 30 passed before the final logical-turn fix. Provider and backend strict mypy pass. Latest production build and desktop/mobile Interviews flows: 2 passed, including draft revision and repeat source retention.
- Real ElevenLabs account access, TTS generation and prior-recording transcription succeeded. Both the short uploaded fixture and the complete 33.6-second spoken-answer fixture are batch transcribed accurately.
- Real Jev Decisions API returned `typesafe/jev-1.13-20260917`: deliberately vague answer 25/100; specific measured answer 75/100. Explanations use `anthropic/claude-sonnet-4.6`, cannot change grades and retain only exact answer quotations.
- Native real-room smoke `11ac604fbd024420abd28014e88ca31a`: 7,960 incoming audio frames, 3,525 non-silent frames, two persisted and graded turns, manual stop silences audio. This earlier smoke exposed fragmented speech and is not final proof of full-answer capture.
- Actual browser at `http://127.0.0.1:4327/interviews` through Next gateway to Python `8775`: created `e79386fd3cea44f98777eb5d9754679d`, uploaded MP3, obtained ready source chunk, and generated opening question explicitly referring to the uploaded prior round's incrementality/branded-search discussion. Real LiveKit browser connection, received speech, synthetic microphone speech, Jev scoring, mute and manual end all observed. Full spoken-answer capture still required a fix; the test's 25/100 is not a valid assessment of its intended full answer.
- Real browser repeat retained the uploaded recording. Permission-denial injection produced an explicit error and microphone off state, without fabricating a connected voice session.

## Issues found and fixed during acceptance

- macOS multiprocessing handler must be module-level; pickling regression added.
- Non-chat LiveKit events cannot be treated as transcript messages.
- Jev per-dimension confidence and optional prose evidence must match backend schema.
- End/shutdown must drain judgments after audio closes.
- Loopback LiveKit needs `rtc.enable_loopback_candidate: true` for browser ICE; both HTTP services remain loopback-only.
- A room dispatch created while no worker is available may never acquire a job; explicit reconnect now recycles stale unassigned/terminal dispatches while preserving active agents.
- Drafts can be revised; repeats preserve uploaded evidence; stalled judgments expose retry; PDF/DOCX extraction is bounded; provider failures are visible.
- ElevenLabs realtime manual-commit mode is incompatible with the default LiveKit streaming STT node unless committed explicitly. Full-answer acceptance uses a tested turn-detection configuration, tracked below when verified.

## Full-answer diagnostic finding

Corrected Eleven server-VAD streaming captured all words in native session `7c2a93f4d68942fe98e78478520932c7` and browser session `1e9dc65a0b2247f19b98f6bd26108669`. The browser received 1,132 non-silent monitor samples, rendered real Jev feedback, and stopped audio at the exact 180-second deadline. However, one continuous 33.6-second answer was split into two independently graded turns. This is a failed logical-turn gate, even though no words were lost; the final fix must produce one complete answer and one judgment.

## Corrected native voice gate

Final native room session `b95fd1831e1642e1bedf41a6aaa741d4` passed the strengthened smoke: the entire long answer is one durable turn, exactly one completed Jev grade (50/100), 8,414 received audio frames, 2,852 non-silent frames, audible follow-up and verified silence after manual finish. Receipt: ignored `.imx/interview-helper/logical-turn-receipt.json`. Smoke stderr is empty. An independent Astra reviewer traced the installed SDK: provider final segments accumulate, and local VAD owns logical turn completion.

## Corrected browser voice gate

Browser session `e65f13b4e2474040af9948d8d8711774` used the actual Next gateway, Python service, local LiveKit and funded ElevenLabs/OpenRouter providers. A 33.6-second synthetic microphone answer was captured through its final sentence in **one** durable turn and received **one** completed Jev grade (65/100), with specific weaknesses/actions rendered in the UI. The browser measured 1,266 non-silent incoming audio samples during opening/follow-up monitoring. Mute and reconnect worked, the interviewer audio reattached, and the original deadline remained unchanged. At exactly 180 seconds the session completed with endReason `deadline`; microphone tracks were ended, remote audio elements removed, and the one-answer scorecard survived a page reload. Receipt: ignored `.imx/interview-helper/browser-voice-receipt.json`.

All five implementation slices are ready for the personal mock. Prior-round recordings and transcripts are optional for every round. Leo can practice with the job, resume/profile and any supplied notes, and should review the editable duration/interviewer-role assumptions. No synthetic score represents Leo's performance.

## Launcher verification

`scripts/run_interview_helper.py --skip-build` started all four actual components on their isolated ports on 2026-10-02. The dashboard loaded through the new launcher. A read-only preflight correctly detected occupied ports and later confirmed availability. Two shutdown-order tests pass; only launcher-owned process groups are eligible for stopping.

## Operational observations

The funded ElevenLabs account responds that zero-retention mode was not applied at its current tier. A request to disable logging is not a guarantee of provider retention behavior. Keys remain in ignored local configuration; the browser receives only a short-lived room token. Native SDK cleanup emitted FFI handle assertions during earlier failed room shutdowns; finished jobs can still emit this nonfatal native SDK destructor warning despite successful audio shutdown and durable grades. It is not suppressed.

## Ready state

Synthetic sessions were copied into owner-only ignored `.imx/interview-helper/acceptance.sqlite3`, then removed by their exact synthetic company labels from the active practice database. The dashboard now contains only the prepared NeuroScale draft. The browser was reloaded to clear synthetic microphone injection and left on that draft. No personal microphone recording or personal mock was performed.

## URL preparation extension evidence

2026-10-02: 267 service tests and 203 frontend tests pass; production build and four desktop/mobile browser tests pass. Provider contract tests exercise Firecrawl-shaped responses through the actual provider/InterviewApi async ingestion path, preserving job text and retrieving company sources. URL validation, bounded page selection, missing credentials, provider failures, retry reuse, legacy compatibility and stale-source replacement are covered. Independent Astra review found no remaining concrete code findings.

Actual browser against the running gateway/service created synthetic URL-only setup e3c81a4c8aff4a379f0c31c070056376 without a pasted description. Missing FIRECRAWL_API_KEY produced the expected recoverable error and disabled Start; receipt is ignored .imx/interview-helper/url-ingestion-receipt.json. Firecrawl has not been called successfully live: the key remains missing from env.local. The failure check is not evidence of real scraping success. Existing personal practices are preserved.
