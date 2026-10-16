# Research and editorial policy

Checked 2026-10-15. Separate published evidence from proposed product behavior.

## Evidence

[Video Repurposing from User Generated Content](https://arxiv.org/html/2412.08879v2)
introduces Repurpose-10K: over 10,000 videos and 120,000 annotated clips. Coarse
LLM proposals were filtered using real user preferences, then creators refined
boundaries. Its baseline fuses visual, audio and caption features and jointly
predicts selection and temporal boundaries. The described feature extraction
uses one-second CLIP ViT-B/32 visuals, PANN audio, WhisperX timestamps and
all-MiniLM-L6-v2 sentence embeddings. This supports multimodal representation and
boundary-aware evaluation; it does not prove that today's commercial product
runs this exact model or that its scores predict social outcomes. This project
does not redistribute the dataset or train that model.

[Opus's score documentation](https://help.opus.pro/docs/article/virality-score)
describes a 0–99 score using Hook, Flow, Value and Trend, with prompt relevance
among additional checks. It does not publish weights, a calibration curve, or a
reproducible scoring formula. Our weights below are our own starting policy.
The page uses likelihood language; that is not evidence that a numerical score
is a calibrated probability.

The user supplied historical GPT-4 curation and independent consistency/context
concerns as background. Those claims have not been independently established by
the two supplied sources. The engine treats context fidelity and reproducibility
as design requirements regardless.

## Loudness hypothesis

[Dhamyal et al.](https://arxiv.org/abs/2211.07737) use pitch, loudness, speech rate
and articulation rate to describe speech for emotion recognition. That is evidence
for acoustic features being informative about delivery; it is not evidence that
louder statements are inherently better clips.

Our engineering hypothesis: a delivery change relative to the same speaker can
help identify emphasis or a reaction when the passage already works semantically.
A raw volume threshold would reward gain differences, music and noise. A quiet,
honest statement may be the best clip.

Current implementation:

- Extract mono 16 kHz PCM and measure RMS dBFS in half-second windows. This is
  an audio-energy proxy, not perceptual LUFS or an emotion detector.
- Use transcript-overlapping windows above a fixed silence floor for baseline
  estimates. When a transcript supplies speaker IDs, use per-speaker medians;
  otherwise use a shared episode median. This does not perform diarization/VAD.
- Give selection the segment's dB difference from that baseline. Do not give
  louder excerpts a separate score bonus. Do not infer semantic emotion labels
  from energy alone. Compression, overlap, noise and mixed audio remain limitations.
- Later test energy as an optional reranking feature using the partners' actual
  keep/reject decisions; preserve both soft and loud clips in the evaluation set.

## Initial editorial policy

Read the whole episode in overlapping transcript windows; retain a summary and
caveats from every part. Propose contiguous mini-stories against that context.
Ground cuts in actual transcript segment IDs. With word-timed input, sentence
segments are formed from real word times. Do not fabricate finer boundaries for
segment-only imports. Reject reversed, unknown and out-of-duration proposals.

Rate Hook, Flow and Value on 0–99 with shared rubric anchors. The aggregate is
`round(.35*hook + .35*flow + .30*value)`, version `editorial-v1`. Trend stays null
until there is dated external evidence and a defined target audience. Do not
rename relevance or model pretraining knowledge as a current trend signal.

Deduplicate substantially overlapping passages, then check candidates against
episode summaries and adjacent speech. Missing or negative reviews exclude a
candidate. This is a model safeguard, not a factual guarantee. Never force the
requested number of clips if too few pass. Rank with stable tie-breaking, retain
raw prompts/results, and cache exact repeated requests within a job.

## Evaluate before expanding

Use a small, private collection of real episodes reviewed independently by both
partners. Include quiet revelations, loud but weak tangents, sarcasm, reversals,
questions answered after a pause, cross-talk and mid-story topic changes.

Measure keep rate among the first few suggestions, missing-context errors,
manual trim displacement, duplicate rate, useful topic coverage, caption
accuracy, runtime and repeat-run stability. Compare transcript-only selection
against selection with relative energy using the same examples and prompts.
Judge the loudness hypothesis from that comparison. Do not optimize a fabricated
virality label. Add scene, laughter and active-speaker signals only after this
baseline produces useful clips.


## Framing implementation

Region selection and visual composition are separate from excerpt ranking.
Game viewports are contained in full so player motion does not require an
unreliable inferred avatar track. People panels are synchronized crops of the
same source, with one original audio track. Two people stay together; a larger
group uses conservative activity evidence or stays as a group.

The optional local baseline uses [OpenCV face detection](https://docs.opencv.org/4.13.0/db/d28/tutorial_cascade_classifier.html)
and head-aligned patch differences. It gates lower-face motion with timestamped
speech and local audio presence, with sustained dominance before focus. It is
not equivalent to a trained audiovisual active-speaker system such as those
evaluated using [AVA ActiveSpeaker](https://research.google/pubs/ava-activespeaker-an-audio-visual-dataset-for-active-speaker-detection/).
Five sampled images and a heuristic cannot guarantee correct framing for every
layout change or speaker turn; source-region review and group/full-source
fallbacks are explicit parts of this implementation.
