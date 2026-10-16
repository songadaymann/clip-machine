# First-slice verification

2026-10-15, macOS Apple Silicon. No personal episode content was used or published.

- 16 standard-library tests pass, including real FFmpeg transcodes for original
  video, portrait video, original audio and captioned audio waveform video.
- JavaScript syntax check passes; installed Python dependencies report no conflicts.
- Editable package installation succeeds in a private Python 3.11 virtualenv.
- MLX Whisper transcribed locally generated speech into six sentence segments
  with word times, preserving the supplied test text.
- A live Codex run on the 96-second synthetic transcript returned three ranked
  clips with grounded boundaries and context reviews.
- A second run from raw generated speech, without a supplied transcript, completed
  local transcription → live Codex selection → captioned waveform export. The
  export is 720×1280 and approximately 18 seconds.
- Claude Code was invoked through its real adapter, but its account returned
  "Spending cap reached" before inference. Its successful selection path remains
  unverified on this account; quota errors are tested and no paid API fallback
  occurs.
- Browser review verified clip selection, start/end edits, keeping a clip,
  export invalidation and revision-2 rendering. Captioned playback and download
  links were present, with no browser console errors observed.
- Browser file import also completed raw audio upload → local transcription →
  live Codex selection → playable 720×1280 captioned waveform export, preserving
  the original display filename.
- At a 390-pixel viewport, the review screen fits without document overflow.
  Rendered interface type sizes remain within the three-size design limit.
- The public repository's Linux GitHub Actions run passed. GitHub secret scanning
  and push protection are enabled; the open-source checker found no blockers.

The sample episode uses explicit fixture rankings, not AI predictions. Generated
speech trials test the wiring; they do not establish selection quality on real
podcasts. Real episode evaluation with both studio partners is still needed.

## Timestamp regression, 2026-10-15

A real recording reproduced the validation failure: nine empty instantaneous
decoder entries and six word alignments that crossed their coarse segment edges
by less than half a second. The normalizer now skips entries without speech and
preserves those bounded word alignments. It still rejects invalid speech times
and larger discrepancies. The full recording normalized successfully with all
canonical segments inside the source duration.

Raw ASR is now cached privately before validation, with audio/model identity
checks, so normalization failures do not force another transcription. All 22
tests and the Linux CI run pass, including coverage for empty entries, coarse
boundary drift, source bounds, cache invalidation and engine integration.

The existing job resumed from its saved upload/transcript, recovered from one
transient provider connection failure using cached analysis, and completed all
20 exports. All exported files were probed: each is 720×1280, matches its selected
duration within the renderer tolerance, and has SRT/VTT/ASS caption sidecars.
Browser playback loaded an exported clip without a media error. Retry handling
also clears a stale error banner once a job resumes.

## Caption upgrade, 2026-10-16

47 tests pass (14 new caption tests plus engine/API coverage), and the JavaScript
syntax check passes. Tests cover cleanup rules and the words they must keep,
balanced phrasing within the measured 580 px line width, override layering,
verbatim mode, segment-only transcripts, ASS escaping, font/accent output,
placement between game and faces, emphasis grounding/spacing, failed requests,
revision/export invalidation and unchanged transcript bytes.

On a private copy of the real game-jam job (not the live job), the 49.5-second
top clip went from 148 spoken words to 119 caption words. One live Codex request
returned ten emphasis words, all at least three seconds apart; an earlier attempt
hit a transient connection failure, which was recorded without failing the job.
The re-export is 720×1280, 49.48 s, one audio stream, with captions centered at
y 706 between the game image (ends at 679) and the camera panels (start at 760).
In the browser, word cycling (emphasized → hidden → shown) cleared overrides on
return, logged feedback and saved the hook. At 375 px the panel had no horizontal
overflow and used only the existing 12/16 px type sizes; no console errors.
Placement and cleanup are heuristics: review captions before sharing.
