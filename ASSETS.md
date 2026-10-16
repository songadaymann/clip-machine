# Assets

Code is MIT; see [LICENSE](LICENSE). This file covers non-code material.

## 1. Ours: included and open

No recorded media is included. Original media made by Jonathan Mann for this
project is CC BY 4.0 unless listed otherwise. Credit Jonathan Mann (Song A Day).
The demo generator creates synthetic tones and test-pattern video locally.

## 2. Free third-party: included under their own licenses

No third-party assets are bundled. System fonts are used. FFmpeg, the transcription model and provider CLIs
are separately installed tools; no binaries or model weights are redistributed.

## 3. Licensed (paid or restricted): not included

None. Put future restricted assets in gitignored `private-assets/` and record
their source URL and terms here when acquired.

## 4. Not ours and not licensed: never in the repo

User episodes, transcripts, speaker identities, generated clips and model outputs
stay in gitignored `data/` or `output/`. Their rights remain with their owners.
Importing media does not grant this project a license to redistribute it.


Optional local face observations use the separately installed
[OpenCV Python distribution](https://github.com/opencv/opencv-python) and its
[frontal-face cascade](https://github.com/opencv/opencv/blob/4.x/data/haarcascades/haarcascade_frontalface_default.xml).
The classifier file carries its own Intel/OpenCV license notice; OpenCV 4.5+
uses [Apache 2.0](https://opencv.org/license/). No classifier weights are copied
into this repository. [Pillow](https://python-pillow.github.io/) is used for
verification under its [HPND license](https://github.com/python-pillow/Pillow/blob/main/LICENSE).
