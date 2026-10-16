# Studio integration

The product direction is a future recording → editing → sharing podcast studio.
This repository owns a clipping stage; the studio's other stages are independent.

```text
Recording / editor
  │ local episode path + optional timestamped transcript + editorial direction
  ▼
Media inspection → transcript normalization → aligned energy representation
  ▼
Whole-episode context → candidate passages → ranking / duplicate suppression
  ▼
Context audit → reviewable clip manifest
  │ original suggestions + manual trim/keep/reject feedback
  ▼
FFmpeg renderer + relative captions → export artifacts
  │ MP4 / M4A + SRT / VTT / ASS + metadata
  ▼
Studio library / sharing stage
```

## Modules

| Module | Owns | Replace/extend without changing |
| --- | --- | --- |
| `media.py` | Probe, audio extraction, energy windows | Editorial selection |
| `transcript.py` | Canonical schema, import, optional MLX | Renderer, review UI |
| `providers.py` | Model interface, Codex/Claude adapters, request cache | Media processing |
| `selection.py` | Prompts, grounding, context audit, score policy | Provider transport, rendering |
| `framing.py` | Validated source-region/timeline contract and layout rules | Visual model, renderer |
| `vision.py`, `tracking.py` | Sampled image interpretation, local face/speaker observations | Editorial selection, UI |
| `render.py` | Accurate transcoding, layouts, captions | Editorial model |
| `storage.py` | Private local artifact store, atomic manifests | User interface |
| `engine.py` | Job orchestration and review/export entry points | Studio UI and social publishing |
| `server.py`, `web/` | Optional loopback review adapter | Engine callers |

`analyze_job(..., provider=..., transcriber=...)` accepts independent provider
implementations. `select_clips` can also run directly on a canonical transcript
and aligned feature data; `render_clip` can render a reviewed manifest without
calling any model. There is no framework or cloud-service dependency in the core.

The current job store and renderer materialize local files. Cloud integration
will need a worker to fetch an object-storage source and persist artifacts, plus
a durable job queue. `JobStore` is not advertised as a drop-in remote database
interface. Keep storage references separate from the studio's logical episode ID.
The CLI references the source directly; browser imports copy it to local storage.
Deleting the working directory is an explicit user action, not an automatic
cleanup task.

## Transcript contract, version 1

Import accepts a Whisper/WhisperX object with `segments`:

```json
{
  "language": "en",
  "segments": [
    {
      "start": 0.0,
      "end": 3.2,
      "speaker": "host",
      "text": "Here is a complete sentence.",
      "words": [
        {"start": 0.0, "end": 0.2, "word": "Here"}
      ]
    }
  ]
}
```

`words` and `speaker` are optional. Provide a complete word list if supplying
words; do not truncate the text's alignment like this abbreviated illustration.
Unaligned WhisperX words cause fallback to the segment text. Imported speech
must be on the exact media timeline passed into this engine. If the editor
removes passages, it must export matching media/transcript times, or map the
transcript onto the edited media first.

The canonical transcript has stable `s000000` IDs, `schema_version: 1`, speaker,
text, start/end seconds and optional word timings. All times refer to the source
episode, never to a generated clip. Segments with word times are grouped into
sentences or bounded phrases. Imported segment-only text remains segment-level.
Empty decoder entries (including instantaneous entries with equal start/end
times) are omitted before validating speech timestamps. Content-bearing entries
must still have valid timestamps. MLX's raw result is saved privately before
normalization, keyed by the model and decoded audio's size/mtime, so a validation
failure can be retried without repeating transcription.
Precise word alignment may extend up to half a second beyond coarse segment
edges; those word times are preserved instead of being cut to the coarse edges.
They must still fit the source timeline (with a small rounding allowance at the
end, clamped to the media duration). Larger discrepancies are rejected.
For multi-track files, ingestion and rendering use the same default-marked audio
stream, or the first audio stream if none is marked default. There is no automatic
multi-track mix; the recording/editor stage should supply its intended episode mix.

## Job / clip contract

`job.json` has schema version, job ID, source metadata, options, lifecycle status,
events, provider/prompt provenance, episode context, clips, rejected candidates
and feedback. Paths in this local manifest are private machine-local references.

Each clip contains:

- A stable ID derived from its proposed transcript boundaries; title and topic.
- Current and original start/end seconds; original segment IDs; revision.
- Aggregate score, component scores (`trend: null`), score version, editorial
  reason and context-audit note. These apply to the original proposal.
- Review decision: `unreviewed`, `approved`, or `rejected`.
- Null export, or export paths, dimensions, duration, layout, caption mode and
  rendered revision. Changing boundaries clears the current export reference.

All clips are contiguous. There is no hidden word removal, content reordering,
or invented speech. Exporting all includes unreviewed and approved clips and
skips rejected ones. Old revision files remain private in the job folder.

Lifecycle: queued → inspecting → preparing → transcribing → analyzing →
summarizing → selecting → checking → ready → framing (optional) → rendering → complete. Failure
stores a readable error. A successful run can yield zero clips. Retry analysis
with cached inputs before clips exist; retry export once clips exist. Interrupted
jobs become retryable on server restart. The local server queues work serially;
library callers should serialize operations on a job.

## Local HTTP adapter

Bind: `127.0.0.1:8787`, no CORS access. `GET /api/config` returns capabilities
and a process-scoped write token. Every POST requires `X-Clip-Token` and an
allowed Host/Origin. This adapter is intended for one trusted local user.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/jobs` | List local episode jobs |
| `GET /api/jobs/:id` | Job manifest and local busy state |
| `GET /api/jobs/:id/transcript` | Canonical transcript |
| `GET /api/jobs/:id/energy` | Energy windows and segment features |
| `GET /api/jobs/:id/source` | Range-capable source playback |
| `GET /api/jobs/:id/files/:name` | Current exported artifact only |
| `POST /api/upload?name=episode.mp4` | Raw file body → local upload ID |
| `POST /api/jobs` | `{source: upload_id, transcript: upload_id_or_null, options: {...}}` → queued job |
| `POST /api/jobs/:id/edit` | `{clip_id, start?, end?, decision?}` → updated manifest |
| `POST /api/jobs/:id/export` | `{clip_id?}` → background render |
| `POST /api/jobs/:id/retry` | Retry failed analysis |
| `POST /api/demo` | Create clearly labeled synthetic demo |

For orchestration on this computer, importing the engine or invoking the CLI
avoids upload copies. A remote/public API would need authentication, quotas,
durable storage, a queue, cancellation and sandboxed workers. Do not expose this
local server to the network as that API.

## Next modules

1. Evaluate real episodes together and use the saved trim/keep/reject feedback.
2. Evaluate framing on varied layouts; replace heuristic activity with a trained
   audiovisual active-speaker model and scene-change tracking.
3. Add time-aligned visual and acoustic events through explicit feature modules.
4. Introduce actual dated trend evidence if it proves useful for the studio.
5. Add remote workers/storage and the studio's final publishing integration.


## Framing contract, version 1

`clip.framing` is independent of editorial scores. It binds `clip_start/clip_end`
to the current trim, records `policy`, `method`, `needs_review`, sampled source
images, evidence and a contiguous relative-time `scenes` list. Each scene has
normalized `[x,y,width,height]` source regions (anonymous `person`, `gameplay`,
`content`) and panels mapping a source crop to `[x,y,width,height]` destination
pixels on a 720×1280 canvas. All panels use `fit: contain`; the bottom 176 pixels
are reserved for captions. Protected subject boxes must remain inside the crop;
game/content regions are preserved in full. Destination panels cannot overlap.
A `full_source` fallback must preserve the complete source image.

Two-person scenes retain both; content scenes retain all participants. Larger
speaker scenes require an explicit active person. The renderer validates the
contract without invoking models. `VisualFramer` implements `analyze(source,
media, clip, folder, policy, audio_path, transcript)`; `reframe_job(..., framer=...)`
accepts an alternative implementation for the studio. `save_framing` creates a
manual whole-clip plan. Changed plans increment revision and clear the export;
trim edits archive/clear stale framing. Previous plans/exports stay private.

Visual interpretation currently samples five images per clip, keeping a full
source view across intervals with inconsistent geometry. Local observations sample
faces twice per second within known feeds. Stable face bounds protect heads;
audio-gated, head-aligned lower-face motion can suggest a speaker in groups of
three or more. Focus requires sustained activity and dominance over other feeds.
This is not diarization, identity recognition or trained audiovisual speech
classification. Missing dependencies/detections and ambiguous turns retain the
group. Frames are sent through the selected CLI, cached by image content as well
as prompt/schema/model. No model calls happen in automated tests.

HTTP additions:

- `POST /api/jobs/:id/frame`: `{clip_id?, policy?}` queues analysis and re-export.
- `POST /api/jobs/:id/framing`: `{clip_id, regions, policy?}` saves manual framing.
- `GET /api/jobs/:id/framing/:revision/:image.jpg`: current plan's whitelisted
  sample/preview images only. Arbitrary private job files cannot be downloaded.

A framing failure preserves the current export and exposes the error for retry.
Auto framing runs only when `options.smart_framing` is true for portrait video.
Without a plan the renderer preserves the full source rather than center-cropping.
