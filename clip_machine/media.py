"""Media operations use separately installed FFmpeg executables, never a shell."""
from __future__ import annotations

import array
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import uuid
import wave
from pathlib import Path


def run(command, **kwargs):
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=kwargs.pop("timeout", 7200), **kwargs)
    except FileNotFoundError:
        raise RuntimeError("Install %s and make it available on PATH" % command[0]) from None
    if result.returncode:
        raise RuntimeError("%s failed: %s" % (Path(command[0]).name,
                           result.stderr.decode(errors="replace")[-3000:]))
    return result.stdout


def probe(source):
    raw = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                          "-of", "json", str(source)]))
    streams = raw.get("streams", [])
    if not any(s["codec_type"] == "audio" for s in streams):
        raise ValueError("This version needs an audio track")
    duration = float(raw.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Could not determine the media duration")
    video = next((s for s in streams if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio_streams = [s for s in streams if s["codec_type"] == "audio"]
    audio = next((s for s in audio_streams if s.get("disposition", {}).get("default")), audio_streams[0])
    rotation = next((s.get("rotation", 0) for s in (video or {}).get("side_data_list", []) if "rotation" in s),
                    (video or {}).get("tags", {}).get("rotate", 0))
    width, height = ((video.get("width"), video.get("height")) if video else (None, None))
    if video and abs(float(rotation)) % 180 == 90:
        width, height = height, width  # FFmpeg and OpenCV apply display rotation before framing.
    return {"duration": duration, "has_video": bool(video),
            "audio_stream": audio["index"], "video_stream": video["index"] if video else None,
            "width": width, "height": height}


def extract_audio(source, target, audio_stream=None):
    target = Path(target)
    temporary = target.with_name("audio-" + uuid.uuid4().hex + ".wav")
    try:
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source), "-vn",
             "-map", "0:%d" % audio_stream if audio_stream is not None else "0:a:0",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary)])
        os.replace(str(temporary), str(target))
    finally:
        temporary.unlink(missing_ok=True)


def energy_features(wav_path, segments):
    """Half-second RMS dBFS; baseline uses speech windows per supplied speaker.

    This is an energy proxy, not perceptual LUFS, emotion, or a shareability score.
    """
    windows = []
    with wave.open(str(wav_path), "rb") as audio:
        rate = audio.getframerate()
        while True:
            frames = audio.readframes(rate // 2)
            if not frames:
                break
            values = array.array("h", frames)
            if sys.byteorder != "little":
                values.byteswap()
            rms = math.sqrt(sum(v * v for v in values) / len(values)) / 32768
            windows.append({"start": len(windows) * .5,
                            "dbfs": round(20 * math.log10(max(rms, 1e-6)), 2)})
    baselines = {}
    samples = {}
    for segment in segments:
        selected = windows[int(segment["start"] * 2):max(int(segment["start"] * 2) + 1,
                                                    math.ceil(segment["end"] * 2))]
        samples[segment["id"]] = [w["dbfs"] for w in selected if w["dbfs"] > -60]
        speaker = segment.get("speaker") or "unknown"
        baselines.setdefault(speaker, []).extend(samples[segment["id"]])
    medians = {s: statistics.median(values) if values else -60 for s, values in baselines.items()}
    result = {}
    for segment in segments:
        values = samples[segment["id"]]
        speaker = segment.get("speaker") or "unknown"
        result[segment["id"]] = {
            "relative_db": round(statistics.median(values) - medians[speaker], 2) if values else 0,
            "baseline": "speaker" if segment.get("speaker") else "episode",
        }
    return {"window_seconds": .5, "windows": windows, "segments": result,
            "kind": "rms_dbfs", "speaker_baselines": medians}


def capabilities():
    import importlib.util
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
            "codex": bool(shutil.which("codex")), "claude": bool(shutil.which("claude")),
            "mlx": importlib.util.find_spec("mlx_whisper") is not None}
