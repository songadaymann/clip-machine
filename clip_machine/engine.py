"""Studio entry points: create a job, analyze it, review clips, export artifacts."""
from __future__ import annotations

import json
from pathlib import Path

from .captions import caption_plan, caption_settings, episode_words, suggest_emphasis, validate_caption_edit
from .media import energy_features, extract_audio, probe
from .providers import CLIProvider, CachedProvider
from .render import render_clip, framing_preview
from .framing import POLICIES, fallback_plan, manual_plan, validate_plan
from .vision import VisualFramer
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
                    job["options"].get("transcription_model", "mlx-community/whisper-large-v3-turbo"),
                    raw_cache=folder / "asr-cache.json")
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
        emphasis_failed = False
        if job["clips"]:
            store.event(job, "captioning", "Choosing caption emphasis")
            for clip, suggestion in _emphasis(transcript, job["clips"], cached):
                clip["captions"] = {"emphasis_suggestion": suggestion}
                emphasis_failed = emphasis_failed or bool(suggestion["error"])
        job["analysis"]["finished_at"] = now()
        store.event(job, "ready", "%d clips ready to review%s" % (len(job["clips"]),
                    " (caption emphasis unavailable; retry from Captions)" if emphasis_failed else "") if job["clips"] else
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
            if clip.get("framing"):
                clip.setdefault("framing_history", []).append(clip.pop("framing"))
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
            if (job["options"].get("smart_framing") and job["options"].get("layout") == "portrait"
                    and job["media"]["has_video"] and not clip.get("framing")):
                _analyze_framing(store, job, clip, transcript=transcript)
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


def _ready_framing(job):
    if job["status"] not in ("ready", "complete", "failed") or not job.get("media", {}).get("has_video"):
        raise ValueError("Wait for a video episode to finish before framing")


def _set_framing(clip, plan):
    if clip.get("framing"):
        clip.setdefault("framing_history", []).append(clip["framing"])
    clip["framing"] = plan
    clip["revision"] += 1
    clip["export"] = None


def _analyze_framing(store, job, clip, policy="auto", framer=None, transcript=None):
    folder = store.folder(job["id"])
    target = folder / "framing" / (clip["id"] + "-r%d" % (clip["revision"] + 1))
    target.mkdir(parents=True, exist_ok=True)
    store.event(job, "framing", "Choosing a layout: " + clip["title"])
    if transcript is None:
        transcript = json.loads((folder / "transcript.json").read_text())
    if policy == "full_frame":
        plan = fallback_plan(clip, "Full source view selected.")
        plan.update(needs_review=False, method="full-source-reviewed")
    elif framer is None:
        provider = CLIProvider(job["options"].get("provider", "codex"), job["options"].get("model"))
        framer = VisualFramer(CachedProvider(provider, target / "model-cache"))
    # A failed analysis leaves the current reviewed plan/export untouched.
    if policy != "full_frame":
        plan = framer.analyze(verify_source(job), job["media"], clip, target, policy,
                              folder / "audio.wav", transcript)
    plan = validate_plan(plan, clip)
    if plan.get("samples"):
        preview = target / "preview.jpg"
        framing_preview(folder / plan["samples"][0]["file"], plan, preview)
        plan["preview"] = str(preview.relative_to(folder))
    write_json(target / "plan.json", plan)
    _set_framing(clip, plan)
    job["options"]["layout"] = "portrait"
    store.save(job)


def reframe_job(store, job_id, clip_id=None, policy="auto", framer=None, render=True):
    job = store.load(job_id)
    _ready_framing(job)
    if policy not in POLICIES:
        raise ValueError("Unknown framing policy")
    selected = [c for c in job["clips"] if c["decision"] != "rejected" and (not clip_id or c["id"] == clip_id)]
    if not selected:
        raise ValueError("No selected clips to frame")
    try:
        for clip in selected:
            _analyze_framing(store, job, clip, policy, framer)
        job["options"]["layout"] = "portrait"
        job["error"] = None
        store.event(job, "ready", "Framing ready to review")
    except Exception as error:
        job["error"] = str(error)
        store.event(job, "failed", str(error))
        raise
    return export_job(store, job_id, clip_id) if render else job


def save_framing(store, job_id, clip_id, regions, policy="auto"):
    with store.lock:
        job = store.load(job_id)
        _ready_framing(job)
        clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
        if clip is None:
            raise ValueError("Unknown clip")
        plan = (fallback_plan(clip, "Full source view selected.") if policy == "full_frame"
                else manual_plan(clip, regions, policy))
        plan.update(needs_review=False)
        previous = clip.get("framing", {})
        plan["samples"] = previous.get("samples", [])
        if plan["samples"]:
            target = store.folder(job_id) / "framing" / (clip_id + "-r%d" % (clip["revision"] + 1))
            target.mkdir(parents=True, exist_ok=True)
            preview = target / "preview.jpg"
            framing_preview(store.folder(job_id) / plan["samples"][0]["file"], plan, preview)
            plan["preview"] = str(preview.relative_to(store.folder(job_id)))
            write_json(target / "plan.json", plan)
        _set_framing(clip, plan)
        job["options"]["layout"] = "portrait"
        job.update(status="ready", error=None, message="Framing saved; export again")
        store.save(job)
        return job


def _emphasis(transcript, clips, provider):
    results = suggest_emphasis(transcript, clips, provider)
    return [(clip, results[clip["id"]]) for clip in clips]


def _set_emphasis(clip, suggestion):
    """Keep earlier suggestions; a failed request never replaces a usable one."""
    captions = clip.setdefault("captions", {})
    previous = captions.get("emphasis_suggestion")
    if suggestion["error"] and previous and not previous.get("error"):
        captions["emphasis_error"] = suggestion["error"]
        return False
    captions.pop("emphasis_error", None)
    if previous and not previous.get("error") and set(suggestion["refs"]) == set(previous["refs"]):
        return False
    if previous:
        captions.setdefault("emphasis_history", []).append(previous)
    captions["emphasis_suggestion"] = suggestion
    if set(suggestion["refs"]) != set((previous or {}).get("refs", [])):
        clip["revision"] += 1
        clip["export"] = None
        return True
    return False


def _caption_clip(job, clip_id):
    if job["status"] not in ("ready", "complete", "failed") or "media" not in job:
        raise ValueError("Wait for processing to finish before editing captions")
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None)
    if clip is None:
        raise ValueError("Unknown clip")
    return clip


def caption_preview(store, job_id, clip_id):
    """The same caption plan the renderer uses, for review and manual edits."""
    job = store.load(job_id)
    clip = _caption_clip(job, clip_id)
    transcript = json.loads((store.folder(job_id) / "transcript.json").read_text())
    plan = caption_plan(transcript, clip, job["options"].get("layout") == "portrait")
    for word in plan["words"]:
        word.pop("trailing", None)
    return {**plan, "clip_id": clip_id, "revision": clip["revision"],
            "emphasis_error": (clip.get("captions") or {}).get("emphasis_error")}


def edit_captions(store, job_id, clip_id, changes):
    """Manual caption overrides layered on the suggestions; the transcript stays untouched."""
    with store.lock:
        job = store.load(job_id)
        clip = _caption_clip(job, clip_id)
        transcript = json.loads((store.folder(job_id) / "transcript.json").read_text())
        validate_caption_edit(changes, {w["ref"] for w in episode_words(transcript)})
        before = json.dumps(caption_settings(clip), sort_keys=True)
        captions = clip.setdefault("captions", {})
        if "mode" in changes:
            captions["mode"] = changes["mode"]
        for key in ("hidden", "emphasis"):
            overrides = captions.setdefault("edits", {}).setdefault(key, {})
            for ref, value in changes.get(key, {}).items():
                if value is None:
                    overrides.pop(ref, None)
                else:
                    overrides[ref] = value
        if "hook" in changes:
            hook = captions.setdefault("hook", {"enabled": False, "text": None})
            if "enabled" in changes["hook"]:
                hook["enabled"] = changes["hook"]["enabled"]
            if "text" in changes["hook"]:
                hook["text"] = (changes["hook"]["text"] or "").strip() or None
        changed = json.dumps(caption_settings(clip), sort_keys=True) != before
        job.setdefault("feedback", []).append({"at": now(), "clip_id": clip_id, "captions": changes})
        if changed:
            clip["revision"] += 1
            clip["export"] = None
        job.update(status="ready", error=None, message="Captions saved; export again" if changed else "Captions unchanged")
        store.save(job)
        return job


def suggest_caption_emphasis(store, job_id, clip_id=None, provider=None):
    """Ask the selected provider for emphasis words on existing clips (cached per request)."""
    job = store.load(job_id)
    if job["status"] not in ("ready", "complete", "failed") or not job["clips"]:
        raise ValueError("Analyze an episode before suggesting caption emphasis")
    selected = [c for c in job["clips"] if c["decision"] != "rejected" and (not clip_id or c["id"] == clip_id)]
    if not selected:
        raise ValueError("No selected clips for caption emphasis")
    folder = store.folder(job_id)
    transcript = json.loads((folder / "transcript.json").read_text())
    provider = provider or CLIProvider(job["options"].get("provider", "codex"), job["options"].get("model"))
    store.event(job, "captioning", "Choosing caption emphasis")
    results = _emphasis(transcript, selected, CachedProvider(provider, folder / "analysis-cache"))
    changed = sum(_set_emphasis(clip, suggestion) for clip, suggestion in results)
    errors = [s["error"] for _, s in results if s["error"]]
    job["error"] = None
    store.event(job, "ready", ("Caption emphasis unavailable: " + errors[0]) if errors else
                "Caption emphasis updated; export again" if changed else "Caption emphasis unchanged")
    return job
