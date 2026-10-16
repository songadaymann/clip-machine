# Clip Machine plan

Build a replaceable clip engine for a future recording → editing → sharing studio.
The surrounding app is evolving; keep its storage, UI and publishing independent.

- [x] Read the supplied research and inspect local tools.
- [x] Confirm integration direction: modular studio component.
- [x] Establish project and documented module contracts.
- [x] Implement media inspection, timestamped transcript import/local transcription,
      audio-energy features, model-driven selection, validation and stable ranking.
- [x] Implement accurate exports, captions, vertical layout and editable boundaries.
- [x] Add a thin local review interface and reproducible demo.
- [x] Verify the pipeline with synthetic media and tests; inspect desktop/mobile UI.
- [x] Run open-source checks, commit and publish the public repository.

First slice: local, transcript-led, contiguous clips, no social publishing.
Later: diarization, active-speaker tracking, richer visual/audio event features,
reference-backed trends, cloud workers and integration with the studio's editor.

## Recording timestamp repair

- [x] Reproduce the failed real-recording transcription and preserve raw ASR output.
- [x] Fix the invalid-timestamp case with regression coverage and retry caching.
- [x] Finish the existing clip job on the restarted local app.
- [x] Verify outputs, run repository checks, and publish the fix.

## Content-aware framing

- [x] Inspect uncropped source frames and define a validated, editable framing contract.
- [x] Add visual scene/region analysis and conservative local tracking/speaker evidence.
- [x] Render synchronized gameplay + people, two-person, group and speaker layouts.
- [x] Add plan review, source-region controls, and export invalidation to the app.
- [x] Verify layout policies and real exports, then publish the modular implementation.

Verified 31 fixture/synthetic tests, including scene transitions, visibility,
one audio track, framing failure recovery, manual edits and private image access.
Reframed and exported the first real episode clip; the app offers Reframe all for
remaining clips. Speaker activity remains a conservative heuristic; sampled
framing cannot recover source occlusions or guarantee unseen scene changes.
