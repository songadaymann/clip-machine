# Clip Machine

A modular, local-first podcast clip engine for a recording → editing → sharing
studio. Give it a full audio/video episode, optionally a timestamped transcript
and editorial direction. Review ranked excerpts, adjust their boundaries, and
export captioned clips.

This is a working first slice: transcript-led selection with whole-episode context
checks, audio-energy features, manual trimming, and original/vertical exports.
Active-speaker tracking, visual understanding and current-trend lookup are future
modules. The surrounding podcast studio can call the Python engine or CLI without
using the bundled review UI.

Live URL: local application at [127.0.0.1:8787](http://127.0.0.1:8787). No hosted service.

## Run locally

Requires Python 3.9+ and separately installed `ffmpeg` and `ffprobe`. Python 3.11+
on Apple Silicon is recommended for optional MLX transcription.

```sh
# macOS, if FFmpeg is missing
brew install ffmpeg

# No Python dependencies are needed for imported transcripts or the synthetic demo.
./scripts/start.sh --open
```

Click **Try a sample episode** to exercise analysis, review, captions and export
without a provider login or model download. This creates test-pattern video and
tones with sample text and fixture rankings. It is labeled synthetic throughout.

For a real episode, install and sign in to [Codex](https://developers.openai.com/codex/cli/)
or [Claude Code](https://code.claude.com/docs/en/quickstart) on this computer.
The app invokes the installed CLI using its existing account login. It does not
extract tokens or treat a subscription as an API key. Account limits still apply;
no automatic paid-API fallback is implemented. These are personal local adapters,
not a shared account-backed service for other users.

To transcribe a full recording automatically on Apple Silicon:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e . mlx-whisper==0.4.3
python -m clip_machine serve --open
```

The default transcription model is `mlx-community/whisper-large-v3-turbo`;
the first real transcription downloads model weights if not cached. Alternatively,
import Whisper/WhisperX JSON from the studio's existing transcription step. Other
operating systems can use imported transcripts. FFmpeg, model weights and provider
CLIs keep their own licenses and are not bundled.

## Use the engine without the UI

```sh
python3 -m clip_machine doctor
python3 -m clip_machine demo
python3 -m clip_machine process /path/to/episode.mp4 \
  --transcript /path/to/whisper.json --provider codex \
  --direction "Complete stories about changing your mind" \
  --count 6 --min-seconds 20 --max-seconds 90 --layout portrait --render
python3 -m clip_machine show JOB_ID
python3 -m clip_machine export JOB_ID
```

Omit `--transcript` to use MLX. Set `--provider claude` for Claude Code. `--model`
can choose a model supported by the installed provider CLI. Run `--help` for
storage, transcription-model and caption options. Install with `pip install -e .`
to also get the `clip-machine` command.

The CLI prints the final JSON manifest on stdout. Jobs and stage events are saved
under `data/jobs/JOB_ID/`; `--data-dir` before the subcommand changes this location.
The browser copies selected files into private local upload storage. The CLI can
reference a recording directly without copying it. Originals are never modified.

## What comes out

- Versioned job JSON with exact source times, title, ranking reasons, context notes
  and Hook/Flow/Value scores. Trend is explicitly unevaluated.
- Captioned H.264/AAC MP4 for video, or for audio with vertical waveform layout.
  Original-layout audio exports to AAC M4A with caption sidecars.
- SRT, WebVTT and ASS captions relative to each clip's start.
- A review history containing original suggestions and manual trim/keep/reject
  feedback. Trimming invalidates an old export; rerendering creates a new revision.

Vertical video uses a **center crop**, which can crop out an off-center speaker.
Preview it before sharing. There is no claim of speaker tracking. Word-timed
transcripts give better caption timing; segment-only imports retain segment-level
captions. ASR and model context checks still need editorial review.

Scores are an editorial ranking, not viral probabilities: `35% Hook + 35% Flow +
30% Value`, rounded to 0–99. Energy informs the prompt but contributes no automatic
score points. The score and context check describe the original proposed cut;
the UI flags manually changed cuts. Missing trend evidence never becomes a
fabricated trend score. See [research and scoring rationale](docs/research.md).

## Integrate with the studio

```python
from clip_machine.storage import JobStore
from clip_machine.engine import create_job, analyze_job, edit_clip, export_job

store = JobStore("data/studio-clips")
job = create_job(store, "data/episode.mp4", {
    "provider": "codex", "count": 6, "layout": "portrait",
}, transcript_path="data/episode-transcript.json")
job = analyze_job(store, job["id"])
first = job["clips"][0]  # Handle an empty result in a production integration.
job = edit_clip(store, job["id"], first["id"], decision="approved")
job = export_job(store, job["id"])
```

Use a queue for long-running calls. Keep the studio's episode IDs alongside job
IDs. Selection has no dependency on rendering or HTTP. Pass a custom
`provider.complete(prompt, schema)` and/or `transcriber.transcribe(audio, duration)`
to `analyze_job` to replace those components. The module boundaries, manifest,
local HTTP endpoints and current integration limits are in
[architecture](docs/architecture.md). There are no social-account or posting
dependencies.

## Privacy and reproducibility

Media processing happens locally. **Transcript text, editorial direction, speaker
labels and energy summaries go to the selected model provider.** Provider
processing and account policies still apply; a subscription is not local inference.
The review server binds only to loopback, validates request origins/hosts, and
requires a per-session token for writes. It is not a public hosting server.

Inputs, transcripts, prompts, provider responses, feedback, and exports are private
working data, excluded from Git. Exact repeated model requests within a job use
cached responses; retry failed analysis with `retry JOB_ID`. A new job can produce
different selections. There is no guarantee of deterministic model inference.
Source size/mtime checks stop exports after the underlying file changes.

## Verify

```sh
python3 -m unittest discover -s tests -v
node --check clip_machine/web/app.js
```

Tests cover time validation, model-boundary grounding, context rejection,
deduplication, cache reuse, gain-invariant relative energy, manual revisions,
real FFmpeg exports and the local API. Tests use synthetic media and fixture
responses; they do not spend model usage or evaluate real-world clip quality.

Code: MIT, copyright Jonathan Mann. Media and excluded inputs: [ASSETS.md](ASSETS.md).
