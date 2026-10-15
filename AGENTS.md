# Project instructions

Keep the clipping engine independent from the studio UI and publishing systems.
Read `docs/architecture.md` before changing cross-module contracts. Preserve
original source timestamps and original suggestions alongside manual edits.

This repository is public by default. Code is MIT, copyright Jonathan Mann.
Never commit credentials, imported episodes, transcripts, model responses,
speaker/user data, exports, model weights, paid asset packs or generated output.
Use gitignored `data/`, `output/` and `private-assets/`; document asset provenance
in `ASSETS.md`. Keep paths portable and document configuration in the README.
Before public pushes, run the `start-open-project` skill's `scripts/oss-check.sh`
and fix every BLOCK.

Run `python3 -m unittest discover -s tests -v` for engine changes and
`node --check clip_machine/web/app.js` for UI changes. FFmpeg is required for
integration tests. Synthetic demos must be clearly labeled. Do not spend account
usage in automated tests; use provider fixtures.

Scores are rankings, not probabilities. Do not fabricate trend evidence or claim
that center cropping is active-speaker tracking. Keep model/provider login
credentials under the installed CLI's control.
