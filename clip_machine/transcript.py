"""Canonical timestamped transcript shared with the studio, editor, and renderer."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .storage import write_json

# Whisper's coarse segment boundaries can differ slightly from forced word
# alignment. Keep the more precise word times, allowing bounded edge drift.
WORD_BOUNDARY_TOLERANCE = .5


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Timestamps must be finite numbers in seconds")
    return float(value)


def normalize(raw, duration):
    if not isinstance(raw, dict) or not isinstance(raw.get("segments"), list):
        raise ValueError("Transcript JSON needs a segments array (Whisper/WhisperX format)")
    normalized = []
    for segment in raw["segments"]:
        text = str(segment.get("text", "")).strip()
        # MLX Whisper clears the text/words of instantaneous decoder segments,
        # but leaves their start == end records in the returned segments list.
        # They contain no speech to align and must be filtered before validating
        # the timestamps of actual transcript content.
        if not text and not segment.get("words"):
            continue
        start, end = number(segment["start"]), number(segment["end"])
        if not (0 <= start < end <= duration + .25):
            raise ValueError("Transcript timestamps extend beyond the episode or are reversed")
        speaker = segment.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise ValueError("Speaker IDs must be strings")
        words = []
        for word in segment.get("words", []):
            # WhisperX sometimes omits word times for unaligned tokens. Retain the
            # segment text, but do not invent precise timing for those tokens.
            if "start" not in word or "end" not in word:
                words = []
                break
            ws, we = number(word["start"]), number(word["end"])
            if not (0 <= ws <= we <= duration + .25 and
                    start - WORD_BOUNDARY_TOLERANCE <= ws <= we <= end + WORD_BOUNDARY_TOLERANCE):
                raise ValueError("Word timestamps must be within the episode and close to their segment")
            words.append({"start": min(duration, ws), "end": min(duration, we),
                          "text": str(word.get("word", word.get("text", ""))).strip()})
        if not text and not words:
            continue
        if words:
            group = []
            for word in words:
                group.append(word)
                if re.search(r"[.!?][\"'”’)]*$", word["text"]) or word["end"] - group[0]["start"] >= 12:
                    _append(normalized, group, speaker)
                    group = []
            if group:
                _append(normalized, group, speaker)
        else:
            normalized.append({"start": start, "end": min(duration, end),
                               "text": text, "speaker": speaker, "words": []})
    normalized.sort(key=lambda s: (s["start"], s["end"]))
    if not normalized:
        raise ValueError("No timestamped speech found")
    for i, segment in enumerate(normalized):
        segment["id"] = "s%06d" % i
    return {"schema_version": 1, "language": raw.get("language", "unknown"),
            "segments": normalized, "timing": "word" if all(s["words"] for s in normalized) else "segment"}


def _append(out, words, speaker):
    if words[-1]["end"] <= words[0]["start"]:
        return
    out.append({"start": words[0]["start"], "end": words[-1]["end"],
                "text": " ".join(w["text"] for w in words), "words": words, "speaker": speaker})


class ImportedTranscript:
    name = "imported"

    def __init__(self, path):
        self.path = Path(path)

    def transcribe(self, audio, duration):
        return normalize(json.loads(self.path.read_text()), duration)


class MLXTranscriber:
    name = "mlx-whisper"

    def __init__(self, model="mlx-community/whisper-large-v3-turbo", raw_cache=None):
        self.model = model
        self.raw_cache = Path(raw_cache) if raw_cache is not None else None

    def transcribe(self, audio, duration):
        stat = Path(audio).stat()
        signature = {"model": self.model, "audio_size": stat.st_size, "audio_mtime_ns": stat.st_mtime_ns}
        if self.raw_cache is not None and self.raw_cache.exists():
            cached = json.loads(self.raw_cache.read_text())
            if cached.get("signature") == signature:
                return normalize(cached["result"], duration)
        try:
            import mlx_whisper
        except ImportError:
            raise RuntimeError("Install mlx-whisper in this Python environment (Apple Silicon), "
                               "or import a timestamped transcript JSON") from None
        raw = mlx_whisper.transcribe(str(audio), path_or_hf_repo=self.model, word_timestamps=True)
        # Preserve expensive ASR output even if canonical validation fails. A
        # retry after a normalizer fix can reuse it without another model run.
        if self.raw_cache is not None:
            write_json(self.raw_cache, {"signature": signature, "result": raw})
        return normalize(raw, duration)


def chunks(segments, max_chars=14000, overlap_seconds=90):
    current, size = [], 0
    for segment in segments:
        cost = len(segment["text"]) + 120
        if current and size + cost > max_chars:
            yield current
            cutoff = current[-1]["end"] - overlap_seconds
            current = [s for s in current if s["end"] > cutoff]
            # Long individual segments must not trap the chunker in a full overlap.
            while len(current) > 1 and sum(len(s["text"]) + 120 for s in current) > max_chars * .9:
                current.pop(0)
            size = sum(len(s["text"]) + 120 for s in current)
        current.append(segment)
        size += cost
    if current:
        yield current
