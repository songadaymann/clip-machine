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
