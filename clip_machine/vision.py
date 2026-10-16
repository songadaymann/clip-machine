"""Sampled visual interpretation → protected source regions → framing plan."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .framing import choose_layout, fallback_plan, panels_for, validate_plan, validate_regions
from .media import run
from .selection import object_schema, validate_schema
from .storage import write_json
from .tracking import face_window, observe, union_rect


BOX = {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 1000}}
REGION_SCHEMA = object_schema({"id": {"type": "string"}, "kind": {"type": "string", "enum": ["person", "gameplay", "content"]},
    "label": {"type": "string"}, "rect": BOX, "subject": BOX,
    "confidence": {"type": "integer", "minimum": 0, "maximum": 100}})
VISUAL_SCHEMA = object_schema({"frames": {"type": "array", "items": object_schema({
    "index": {"type": "integer"}, "regions": {"type": "array", "items": REGION_SCHEMA},
    "note": {"type": "string"}})}})


def sample_frames(source, media, clip, folder, count=5):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    duration = clip["end"] - clip["start"]
    samples = []
    for i in range(count):
        relative = duration * i / (count - 1)
        actual = min(clip["end"] - .05, clip["start"] + max(.01, relative))
        path = folder / ("source-%02d.jpg" % i)
        if not path.exists():
            run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(actual), "-i", str(source),
                 "-map", "0:%d" % media["video_stream"], "-frames:v", "1", "-vf", "scale=1280:-2", str(path)])
        samples.append({"index": i, "time": relative, "source_time": actual, "path": str(path)})
    return samples


def decode_regions(frame):
    regions = []
    # A questionable participant must not quietly disappear from the composition.
    if any(raw.get("confidence", 0) < 55 for raw in frame.get("regions", [])):
        return []
    for raw in frame.get("regions", []):
        if raw.get("confidence", 0) < 55:
            continue
        regions.append({"id": raw["id"], "kind": raw["kind"], "label": raw["label"],
                        "rect": [v / 1000 for v in raw["rect"]],
                        "subject": [v / 1000 for v in raw["subject"]] if raw.get("subject") else None})
    return validate_regions(regions) if regions else []


def same_geometry(a, b):
    left, right = {r["id"]: r for r in a}, {r["id"]: r for r in b}
    return (bool(left) and left.keys() == right.keys() and
            all(left[k]["kind"] == right[k]["kind"] and
                max(abs(x - y) for x, y in zip(left[k]["rect"], right[k]["rect"])) < .035 for k in left))


def combined_regions(groups):
    result = copy.deepcopy(groups[0])
    for region in result:
        matches = [next(r for r in group if r["id"] == region["id"]) for group in groups]
        region["rect"] = union_rect([r["rect"] for r in matches])
        subjects = [r["subject"] for r in matches if r.get("subject")]
        region["subject"] = union_rect(subjects) if subjects else None
    return validate_regions(result)


def make_scene(start, end, regions, policy, media, evidence=None, active=None):
    regions = copy.deepcopy(regions)
    layout = choose_layout(regions, policy, active)
    panels = panels_for(regions, layout, active)
    lookup = {r["id"]: r for r in regions}
    for panel in panels:
        region = lookup.get(panel["region_id"])
        if region and region["kind"] == "person":
            observations = (evidence or {}).get("face_tracks", {}).get(region["id"], [])
            subjects = [o["rect"] for o in observations if start <= o["time"] <= end and o.get("detected")]
            if region.get("subject"):
                subjects.append(region["subject"])
            panel["source"] = face_window(region, subjects, panel["destination"], media)
            if subjects:
                region["subject"] = union_rect(subjects)
    reason = ("Keep the complete game/content area and every person visible." if layout == "gameplay_people"
              else "Keep both people visible." if layout == "two_people"
              else "Sustained speaker evidence permits focus; switch back to the group when uncertain." if active
              else "Keep the group visible; speaker evidence is uncertain." if layout == "group"
              else "Preserve the visible source subject.")
    return {"start": start, "end": end, "regions": regions, "layout": layout,
            "active_person": active, "panels": panels, "reason": reason}


def speaker_scenes(duration, regions, policy, media, evidence):
    timeline = evidence.get("speaker_timeline", [])
    if not timeline or policy == "group" or any(r["kind"] in ("gameplay", "content") for r in regions):
        return [make_scene(0, duration, regions, policy, media, evidence)]
    turns = []
    for item in timeline:
        person = item["person_id"]
        if not turns or person != turns[-1]["person_id"]:
            turns.append({"time": item["time"], "person_id": person})
    if turns[0]["time"] > 0:
        turns.insert(0, {"time": 0, "person_id": None})
    return [make_scene(turn["time"], turns[i + 1]["time"] if i + 1 < len(turns) else duration,
                       copy.deepcopy(regions), policy, media, evidence, turn["person_id"])
            for i, turn in enumerate(turns) if turn["time"] < duration]


class VisualFramer:
    def __init__(self, provider):
        self.provider = provider

    def analyze(self, source, media, clip, folder, policy="auto", audio_path=None, transcript=None):
        if policy == "full_frame":
            plan = fallback_plan(clip, "Full source view selected.")
            plan.update(needs_review=False, method="full-source-reviewed")
            return plan
        samples = sample_frames(source, media, clip, folder)
        prompt = """Plan source regions for a vertical video composition. Inspect the supplied frames in order.
Return exactly one entry per frame, with index 0..4. Rectangles are [x,y,width,height]
in integer 0..1000 coordinates of the original supplied image (not output positions).
Identify camera FEEDS and one essential gameplay/screen/content viewport. Exclude
browser tabs, chats and surrounding desktop when the actual game viewport is clear.
When it isn't clear, include the complete application/content area rather than
risking cutting off the player avatar. The FULL game region will be preserved.
For camera feeds, include the head/shoulders, not a tiny tight face-only rectangle.
Put a tighter protected face rectangle in subject when clear. For a game, subject
may be the visible avatar; use [] if it cannot be reliably located. Never invent it.
Include all distinct participant feeds even when a person is not speaking. Ignore
nested self-preview thumbnails/duplicate screenshare previews and prefer the largest
original camera feed. Do not identify or name people; use anonymous feed IDs/labels
(person_1 etc.). Keep feed IDs consistent across frames using camera position/layout,
not biometric identity. Do not infer speaking from a still face. Prefer wider bounds
when uncertain. Main content uses kind gameplay or content; choose at most ONE.
No more than eight regions per frame. No logos, illustrated characters or photos as
human participants. A genuinely unknown layout may return no regions. Give a short
note about ambiguity, changes, or occlusion. Confidence describes region evidence,
not an outcome probability. Desired policy: %s.
Frame source times: %s
""" % (policy, json.dumps([{k: s[k] for k in ("index", "time", "source_time")} for s in samples]))
        response = validate_schema(self.provider.complete(prompt, VISUAL_SCHEMA, images=[s["path"] for s in samples]), VISUAL_SCHEMA)
        write_json(Path(folder) / "visual-response.json", response)
        decoded = {}
        for frame in response.get("frames", []):
            index = frame.get("index")
            if type(index) is not int or not 0 <= index < len(samples) or index in decoded:
                raise ValueError("Visual model returned unknown or duplicate frame indices")
            decoded[index] = decode_regions(frame)
        groups = [decoded.get(i, []) for i in range(len(samples))]
        duration = clip["end"] - clip["start"]
        plan = {"schema_version": 1, "clip_start": clip["start"], "clip_end": clip["end"],
                "policy": policy, "method": "sampled-vision-and-local-observations-v1",
                "needs_review": False, "note": "Protected content is composed from synchronized source regions.",
                "samples": [{"index": s["index"], "time": s["time"], "file": "framing/" + Path(folder).name + "/" + Path(s["path"]).name}
                            for s in samples], "evidence": {}, "scenes": []}
        stable = all(same_geometry(groups[0], g) for g in groups[1:])
        if stable:
            regions = combined_regions(groups)
            evidence = observe(source, clip, regions, audio_path, transcript)
            plan["evidence"] = evidence
            plan["scenes"] = speaker_scenes(duration, regions, policy, media, evidence)
        else:
            # A change between samples is not assigned an invented exact cut.
            # Keep the full source in uncertain intervals until a user refines it.
            plan["needs_review"] = True
            plan["note"] = "Layout changes or uncertain regions: changing intervals retain the full source for review."
            for i in range(len(samples) - 1):
                start, end = samples[i]["time"], samples[i + 1]["time"]
                if same_geometry(groups[i], groups[i + 1]):
                    regions = combined_regions([groups[i], groups[i + 1]])
                    plan["scenes"].append(make_scene(start, end, regions, policy, media))
                else:
                    scene = copy.deepcopy(fallback_plan(clip, plan["note"])["scenes"][0])
                    scene.update(start=start, end=end)
                    plan["scenes"].append(scene)
        return validate_plan(plan, clip)
