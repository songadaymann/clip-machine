"""Studio entry points: create a job, analyze it, review clips, export artifacts."""
from __future__ import annotations

import json
from pathlib import Path

from .media import energy_features, extract_audio, probe
from .providers import CLIProvider, CachedProvider
from .render import render_clip
from .selection import select_clips, validate_options
from .storage import now, verify_source, write_json
from .transcript import ImportedTranscript, MLXTranscriber, number


def create_job(store, source, options=None, transcript_path=None):
    options = validate_options(options or {})
    imported = json.loads(Path(transcript_path).read_text()) if transcript_path else None
    job = store.create(source, options)
    if transcript_path:
        write_json(store.folder(job["id"]) / "input-transcript.json", imported)
        job["transcriber"] = "imported"
    else:
        job["transcriber"] = "mlx-whisper"
    store.save(job)
    return job


def analyze_job(store, job_id, provider=None, transcriber=None):
    job = store.load(job_id)
    folder = store.folder(job_id)
    if job["clips"]:
        raise ValueError("This job already has clips. Create a new job to change the analysis.")
    try:
        source = verify_source(job)
        job["error"] = None
        store.event(job, "inspecting", "Reading episode media")
        job["media"] = probe(source)
        audio = folder / "audio.wav"
        if not audio.exists():
            store.event(job, "preparing", "Preparing local audio")
            extract_audio(source, audio, job["media"]["audio_stream"])
        transcript_file = folder / "transcript.json"
        if transcript_file.exists():
            transcript = json.loads(transcript_file.read_text())
        else:
            store.event(job, "transcribing", "Reading timestamped speech")
            if transcriber is None:
                imported = folder / "input-transcript.json"
                transcriber = ImportedTranscript(imported) if imported.exists() else MLXTranscriber(
                    job["options"].get("transcription_model", "mlx-community/whisper-large-v3-turbo"))
            transcript = transcriber.transcribe(audio, job["media"]["duration"])
            write_json(transcript_file, transcript)
        store.event(job, "analyzing", "Measuring delivery against speech baselines")
        energy = energy_features(audio, transcript["segments"])
        write_json(folder / "energy.json", energy)
        job["transcript_timing"] = transcript["timing"]
        provider = provider or CLIProvider(job["options"].get("provider", "codex"), job["options"].get("model"))
        job["analysis"] = {"provider": provider.name, "model": getattr(provider, "model", None),
                           "prompt_version": "editorial-v1", "started_at": now(),
                           "trend": "not evaluated", "energy": "relative RMS dBFS; no score bonus"}
        cached = CachedProvider(provider, folder / "analysis-cache")
        result = select_clips(transcript, energy, job["media"]["duration"], job["options"], cached,
                              lambda status, message: store.event(job, status, message))
        job.update(result)
        job["analysis"]["finished_at"] = now()
        store.event(job, "ready", "%d clips ready to review" % len(job["clips"]) if job["clips"] else
                    "No excerpts passed the requested duration and context checks. Try another direction or duration.")
    except Exception as error:
        job["error"] = str(error)
        store.event(job, "failed", str(error))
        raise
    if job["options"].get("auto_render", False) and job["clips"]:
        return export_job(store, job_id)
    return job


def edit_clip(store, job_id, clip_id, start=None, end=None, decision=None):
    with store.lock:
        job = store.load(job_id)
        if job["status"] not in ("ready", "complete", "failed") or "media" not in job:
            raise ValueError("Wait for processing to finish before editing")
        clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
        if not clip:
            raise ValueError("Unknown clip")
        a = clip["start"] if start is None else number(start)
        b = clip["end"] if end is None else number(end)
        if not (0 <= a < b <= job["media"]["duration"]):
            raise ValueError("Clip boundaries must be inside the episode, with start before end")
        if decision is not None and decision not in ("unreviewed", "approved", "rejected"):
            raise ValueError("Invalid review decision")
        changed = a != clip["start"] or b != clip["end"]
        job.setdefault("feedback", []).append({"at": now(), "clip_id": clip_id,
            "previous_start": clip["start"], "previous_end": clip["end"], "start": a, "end": b,
            "previous_decision": clip["decision"], "decision": decision or clip["decision"]})
        clip.update(start=a, end=b, decision=decision or clip["decision"])
        if changed:
            clip["revision"] += 1
            clip["export"] = None
        job["status"] = "ready"
        job["error"] = None
        job["message"] = "Trim saved; export again" if changed else "Review saved"
        store.save(job)
        return job


def export_job(store, job_id, clip_id=None):
    job = store.load(job_id)
    if job["status"] not in ("ready", "complete", "failed") or not job["clips"]:
        raise ValueError("Analyze an episode before exporting")
    folder = store.folder(job_id)
    try:
        source = verify_source(job)
        transcript = json.loads((folder / "transcript.json").read_text())
        selected = [c for c in job["clips"] if c["decision"] != "rejected" and (not clip_id or c["id"] == clip_id)]
        if not selected:
            raise ValueError("No selected clips to export")
        for index, clip in enumerate(selected):
            store.event(job, "rendering", "Exporting clip %d of %d: %s" % (index + 1, len(selected), clip["title"]))
            if not clip.get("export"):
                clip["export"] = render_clip(source, job["media"], transcript, clip, folder / "exports",
                    job["options"].get("layout", "original"), job["options"].get("burn_captions", True))
                store.save(job)
        job["error"] = None
        store.event(job, "complete", "Exports ready")
        return job
    except Exception as error:
        job["error"] = str(error)
        store.event(job, "failed", str(error))
        raise
