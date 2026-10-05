# Interview Helper operator runbook

The development/rehearsal dashboard is **http://127.0.0.1:4327/interviews**. This stack uses service port **8775**, voice worker health port **8767**, and local LiveKit **7880**. It is separate from the existing Interviewmaxxing dashboard/service on **4317/8765**. The launcher never stops those services or any other pre-existing process.

The native live-provider acceptance passed full-answer capture, one Jev judgment per answer, audible ElevenLabs follow-up and audio stop. The integrated browser evidence is recorded in ACCEPTANCE.md. Synthetic acceptance sessions are test evidence, never Leo's performance. Configuration readiness alone does not prove a live microphone connection.

## Start and stop

From the repository root, after installing the workspace Python dependencies, the web dependencies (`npm ci` inside `apps/web`), Node, and `livekit-server`:

```sh
uv run --package interviewmaxxing-service python scripts/run_interview_helper.py --check
uv run --package interviewmaxxing-service python scripts/run_interview_helper.py
```

The first command only reads configuration and checks local port availability. The second builds the current Next application, then starts owned LiveKit, service, voice worker and dashboard processes. Use `--skip-build` only when `apps/web/.next` is already a current production build. A build is limited to five minutes; each component startup has a timeout. Ctrl-C stops only process groups created by that launcher. It stops web ingress first, then gives the service and voice worker up to 250 seconds to drain pending judgments while LiveKit stays alive, and stops LiveKit last. A second Ctrl-C explicitly forces owned processes to stop and may leave judgments requiring retry. Prefer finishing the practice and confirming final grades before shutdown. Another launcher refuses occupied ports instead of terminating or attaching to unknown processes. Do not run a new build while someone is using the existing production Next process in this same worktree.

The launcher uses:

- `IMX_HOME=<repository>/.imx/interview-helper/runtime` for isolated SQLite sessions and runtime state.
- `IMX_PROFILE_DIR=~/.interviewmaxxing/profile` for the existing canonical candidate profile and selected resume. This references existing evidence; it does not copy or replace the canonical profile.
- `IMX_SERVICE_ORIGIN=http://127.0.0.1:4327`, with a same-origin Next gateway targeting `http://127.0.0.1:8775`.
- `IMX_SERVICE_APPLICATION_MODE=TEST_ONLY` and submission disabled. Interview practice does not require employer application authorization.

The isolated runtime has its own pipeline. It can import tracked jobs already present in that runtime; it does not silently copy the main pipeline database. Enter job details or use the verified NeuroScale source in `docs/interview-helper/neuroscale-job.json` when necessary.

## Provider configuration

Set secrets in ignored root `env.local`, never in browser configuration or tracked files:

```dotenv
OPENROUTER_API_KEY=<your server-side key>
ELEVEN_LABS_API_KEY=<your server-side key>
FIRECRAWL_API_KEY=<your server-side key>
# Optional cloud transport; omit all three for generated local transport credentials.
# LIVEKIT_URL=wss://your-project.livekit.cloud
# LIVEKIT_API_KEY=<your server-side key>
# LIVEKIT_API_SECRET=<your server-side secret>
```

`load_interview_env()` reads the ignored env file and normalizes `ELEVEN_LABS_API_KEY` to `ELEVEN_API_KEY` for the SDK. Explicit process environment values take precedence. `IMX_INTERVIEW_ENV_FILE` can select another private env file. The interviewer and Jev are separate models; Jev uses the Decisions API. The readiness panel distinguishes configuration from actual tested behavior. The launcher does not make paid smoke calls or provision cloud infrastructure.

For local `ws://127.0.0.1:7880`, absent credentials are generated once into ignored owner-only `.imx/interview-helper/local.env`. Existing local credentials are reused. The owner-only `.imx/interview-helper/livekit.yaml` binds loopback and configures RTC TCP **7881**, UDP **50100–50200**, `node_ip: 127.0.0.1`, and **`enable_loopback_candidate: true`**. The latter is required for a local browser to receive usable ICE candidates. All required TCP/UDP ports must be free before launch. Do not expose this local configuration on a public network. Configured remote LiveKit uses the supplied credentials and starts no local transport server.

Inspect rooms without echoing credentials or putting secrets in shell command arguments:

```sh
uv run --package interviewmaxxing-service python - <<'PY'
import os
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from interviewmaxxing_service.interview_providers import load_interview_env
load_interview_env()
local = Path('.imx/interview-helper/local.env')
if local.is_file():
    load_dotenv(local, override=False)
raise SystemExit(subprocess.call(['lk', 'room', 'list'], env=os.environ.copy()))
PY
```

Private logs are in ignored `.imx/interview-helper/logs/`, with owner-only permissions. The launcher redacts loaded credential values and JWT-shaped tokens from child output. Logs and transcripts can still contain personal context; review before sharing. OpenRouter, ElevenLabs and any configured cloud LiveKit receive the relevant requests/media. This implementation does not guarantee provider zero retention; account settings and provider terms govern their handling.

## Prepare the NeuroScale practice

1. Open **Interviews → New interview**. Enter company, role, the **company website URL** and **job application URL**. No pasted job description is required. Saving starts background website import. Firecrawl reads the job page in full and maps the company site, then imports its homepage and up to four relevant About, product, customer or pricing pages. Review the linked sources when processing completes. Failed requested imports block starting and expose **Retry website import**, which preserves successful source work. Existing saved practices can still use their previous context. The target source is `https://jobs.ashbyhq.com/neuroscale/b276f4e7-16f3-4979-acb0-506ac9366328`.
2. Review the reused candidate profile/resume text. If extraction fails, the UI should display the warning; paste text or upload a readable document. A selected resume is evidence, not permission to invent missing achievements.
3. Set round, interviewer role, minutes and target. The prepared **round 2 / hiring manager / 30 minutes** defaults are editable assumptions. They are not confirmation of the actual interviewer or scheduled duration. Use the draft edit control to correct them before starting.
4. Add company/job notes, optional LinkedIn text and previous-round material. Documents support UTF-8 TXT/MD/VTT/SRT, readable PDF and DOCX, up to **5 MB** and **300,000 extracted characters**. Scanned or encrypted PDFs may need manual text extraction. Audio supports MP3/WAV/M4A/OGG/WEBM/FLAC/MP4 up to **25 MB**. Source cards show processing, chunk counts or a recoverable error; uploading the same failed recording retries transcription without duplicating the source.
5. Prior-round recordings and transcripts are optional, including for second and third rounds. With none supplied, practice uses the job description, resume/profile, selected interviewer role and any notes. Uploading real prior-round material allows retrieved statements and unresolved issues to ground subsequent questions and grading. Setup changes and repeat practices preserve separately uploaded evidence.

## Practice and review

Start the timed mock, then explicitly connect voice and allow the browser microphone. Use the exact loopback dashboard URL above. Confirm the microphone, connection state, live transcript and audible interviewer response. If the browser denies the microphone, fix its site permission and reconnect; text mode remains available but does not satisfy voice verification. Do not run two voice practices at once. Finish an active practice before starting another.

The server sets the deadline once. Reconnecting must not extend it. Manual finish or deadline ends the session; the voice worker observes session status and closes its conversation. Text answers and finalized voice answers use request IDs so retries do not create duplicate answers. Per-answer Jev grading runs asynchronously. Pending and failed judgments remain visible; retry failed/stale grading rather than interpreting missing grades as zero or a pass.

The five dimensions are relevance, evidence, structure, credibility and depth, using anchored 0/25/50/75/100 bands. Overall scores are computed from validated dimensions. Judge model, rubric version, confidence and feedback provenance remain attached. A readiness target such as **80/100 is an internal practice benchmark, not an offer probability**. The final report distinguishes short/incomplete practice from full duration. Repeat the same context to compare attempts on the same rubric. Improvements across a few answers are provisional, especially when question coverage differs.

If the question provider fails, retry question generation; do not substitute a fabricated transcript. If transcription, transport or grading is unavailable, inspect the relevant readiness/error message and private component log. Preserve the original session and uploaded context while recovering. The first personal mock requires Leo's microphone participation; prior-round material is optional; synthetic recordings must stay labeled as diagnostics.

## Known runtime limitations

The installed LiveKit native SDK can emit an `FfiHandle.__del__` assertion after a finished job has disconnected. Tests verify durable grades and stopped audio despite this teardown warning. The launcher records it in private logs; it is not suppressed. The tested local setup requires this Mac to keep all four components running. Public hosting and cloud provisioning are outside this local MVP.

## Website ingestion

The new URL setup format sends `companyUrl` and `jobUrl`; both must be nonempty public HTTP(S) URLs. Legacy callers omitting `companyUrl` remain supported. The scraper calls the [Firecrawl v2 scrape endpoint](https://docs.firecrawl.dev/api-reference/endpoint/scrape) and [map endpoint](https://docs.firecrawl.dev/api-reference/endpoint/map) using the server-only `FIRECRAWL_API_KEY`. It rejects private/local targets, credentials, custom ports and unreadable/error pages. It does not fetch arbitrary target URLs from this Mac. Each page is bounded to 100,000 characters; oversized pages fail explicitly rather than silently truncating the job description. Up to five company pages plus the job page are stored with source URLs for retrieval.

Same-URL repeats reuse ready source documents. Changing a URL drops its stale scraped context and imports the replacement. Uploaded prior-round sources and notes remain attached. After changing an API key, restart the launcher; then retry the failed import. No previous-round recording is required.
