"""Validated composition plans. Coordinates are normalized source rectangles.

The renderer consumes this contract without knowing which model/detector made it.
"""
from __future__ import annotations

import copy
import math

from .transcript import number


CANVAS = (720, 1280)
CONTENT_HEIGHT = 1104  # The bottom band stays clear; platform UI covers it. Captions are placed by captions.py.
POLICIES = ("auto", "gameplay_people", "two_people", "group", "speaker", "full_frame")


def rect(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("A region needs [x, y, width, height]")
    x, y, w, h = [number(v) for v in value]
    if not (0 <= x < 1 and 0 <= y < 1 and w >= .001 and h >= .001
            and x + w <= 1.000001 and y + h <= 1.000001):
        raise ValueError("Regions must fit inside the source image")
    return [x, y, min(w, 1 - x), min(h, 1 - y)]


def contains(outer, inner, tolerance=.002):
    return (outer[0] <= inner[0] + tolerance and outer[1] <= inner[1] + tolerance
            and outer[0] + outer[2] >= inner[0] + inner[2] - tolerance
            and outer[1] + outer[3] >= inner[1] + inner[3] - tolerance)


def validate_regions(regions):
    if not isinstance(regions, list) or not 1 <= len(regions) <= 8:
        raise ValueError("Choose between one and eight source regions")
    ids = set()
    result = []
    for raw in regions:
        if not isinstance(raw, dict) or "rect" not in raw:
            raise ValueError("Each source region needs a rectangle and type")
        identity = raw.get("id")
        if not isinstance(identity, str) or not identity or len(identity) > 60 or identity in ids:
            raise ValueError("Source-region IDs must be short and unique")
        ids.add(identity)
        kind = raw.get("kind")
        if kind not in ("person", "gameplay", "content"):
            raise ValueError("Region type must be person, gameplay or content")
        region = {"id": identity, "kind": kind, "label": str(raw.get("label", identity))[:100],
                  "rect": rect(raw["rect"]), "subject": rect(raw["subject"]) if raw.get("subject") else None}
        if region["subject"] and not contains(region["rect"], region["subject"]):
            raise ValueError("A protected subject must be inside its source region")
        result.append(region)
    if sum(r["kind"] in ("gameplay", "content") for r in result) > 1:
        raise ValueError("Choose one main game/content region")
    return result


def choose_layout(regions, policy="auto", active_person=None):
    if policy not in POLICIES:
        raise ValueError("Unknown framing policy")
    people = [r for r in regions if r["kind"] == "person"]
    content = [r for r in regions if r["kind"] in ("gameplay", "content")]
    if policy == "full_frame":
        return "full_frame"
    if policy == "two_people" and len(people) != 2:
        raise ValueError("The two-person layout needs exactly two people")
    if policy == "gameplay_people" and (not content or not people):
        raise ValueError("This layout needs a game/content region and at least one person")
    # Game/content stays present even when one of several people is speaking.
    if content and people:
        return "gameplay_people"
    # Two-person conversations always retain both people, including speaker mode.
    if len(people) == 2:
        return "two_people"
    if len(people) == 1:
        return "speaker"
    if len(people) > 2:
        if active_person in {p["id"] for p in people} and policy != "group":
            return "speaker"
        return "group"
    return "full_frame"


def grid_slots(count, x, y, width, height):
    columns = 1 if count == 1 else 2 if count <= 4 else 3
    rows = math.ceil(count / columns)
    # Even pixel dimensions keep chroma sampling and stacking exact.
    cell_w = (width // columns) // 2 * 2
    cell_h = (height // rows) // 2 * 2
    return [[x + (i % columns) * cell_w, y + (i // columns) * cell_h, cell_w, cell_h]
            for i in range(count)]


def panels_for(regions, layout, active_person=None):
    people = [r for r in regions if r["kind"] == "person"]
    content = [r for r in regions if r["kind"] in ("gameplay", "content")]
    if layout == "full_frame":
        return [{"region_id": "full_source", "source": [0, 0, 1, 1], "destination": [0, 0, 720, CONTENT_HEIGHT], "fit": "contain"}]
    if layout == "gameplay_people":
        main = content[0]
        face_height = 344 if len(people) <= 2 else 480
        slots = grid_slots(len(people), 0, CONTENT_HEIGHT - face_height, 720, face_height)
        panels = [{"region_id": main["id"], "source": main["rect"],
                   "destination": [0, 0, 720, CONTENT_HEIGHT - face_height], "fit": "contain"}]
        panels += [{"region_id": p["id"], "source": p["rect"], "destination": slot, "fit": "contain"}
                   for p, slot in zip(people, slots)]
        return panels
    selected = ([next(p for p in people if p["id"] == active_person)]
                if layout == "speaker" and active_person else people[:1] if layout == "speaker" else people)
    slots = ([[0, 0, 720, 552], [0, 552, 720, 552]] if layout == "two_people"
             else grid_slots(len(selected), 0, 0, 720, CONTENT_HEIGHT))
    return [{"region_id": p["id"], "source": p["rect"], "destination": slot, "fit": "contain"}
            for p, slot in zip(selected, slots)]


def fallback_plan(clip, reason):
    duration = clip["end"] - clip["start"]
    return {"schema_version": 1, "clip_start": clip["start"], "clip_end": clip["end"],
            "policy": "full_frame", "method": "full-source-safety", "needs_review": True,
            "note": reason, "scenes": [{"start": 0, "end": duration, "regions": [],
                "layout": "full_frame", "active_person": None,
                "panels": panels_for([], "full_frame"), "reason": reason}], "samples": [], "evidence": {}}


def validate_plan(plan, clip):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("Unsupported framing plan")
    duration = clip["end"] - clip["start"]
    if abs(number(plan["clip_start"]) - clip["start"]) > .001 or abs(number(plan["clip_end"]) - clip["end"]) > .001:
        raise ValueError("Framing belongs to a different trim; analyze framing again")
    if plan.get("policy") not in POLICIES:
        raise ValueError("Unknown framing policy")
    scenes = plan.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 150:
        raise ValueError("Framing needs a bounded scene timeline")
    previous = 0
    for scene in scenes:
        start, end = number(scene["start"]), number(scene["end"])
        if abs(start - previous) > .001 or not start < end <= duration + .001:
            raise ValueError("Framing scenes must cover the clip without gaps or overlaps")
        previous = end
        regions = validate_regions(scene["regions"]) if scene["regions"] else []
        lookup = {r["id"]: r for r in regions}
        if scene["layout"] not in POLICIES[1:]:
            raise ValueError("Unknown scene layout")
        panels = scene.get("panels", [])
        if not 1 <= len(panels) <= 9:
            raise ValueError("Each scene needs visible source panels")
        shown = set()
        destinations = []
        for panel in panels:
            source = rect(panel["source"])
            shown.add(panel["region_id"])
            x, y, w, h = [number(v) for v in panel["destination"]]
            if not (x >= 0 and y >= 0 and w >= 2 and h >= 2 and x + w <= 720 and y + h <= CONTENT_HEIGHT):
                raise ValueError("Panels must stay inside the canvas and above captions")
            if panel.get("fit") != "contain":
                raise ValueError("Source regions must be contained, never blindly cropped")
            for ox, oy, ow, oh in destinations:
                if min(x + w, ox + ow) > max(x, ox) and min(y + h, oy + oh) > max(y, oy):
                    raise ValueError("Visible panels must not cover each other")
            destinations.append((x, y, w, h))
            if panel["region_id"] == "full_source" and not contains(source, [0, 0, 1, 1]):
                raise ValueError("Full-source fallback must preserve the entire source")
            region = lookup.get(panel["region_id"])
            if panel["region_id"] != "full_source" and region is None:
                raise ValueError("Unknown source region in panel")
            if region and region["kind"] in ("gameplay", "content") and not contains(source, region["rect"]):
                raise ValueError("The game/content view must be preserved in full")
            if region and region.get("subject") and not contains(source, region["subject"]):
                raise ValueError("A protected face/subject would be cropped out")
        if "full_source" not in shown:
            people = {r["id"] for r in regions if r["kind"] == "person"}
            content = {r["id"] for r in regions if r["kind"] in ("gameplay", "content")}
            if len(people) <= 2 or content or scene["layout"] != "speaker":
                if not people.issubset(shown):
                    raise ValueError("This composition must retain all visible people")
            if not content.issubset(shown):
                raise ValueError("The game/content region must stay on screen")
            if len(people) > 2 and scene["layout"] == "speaker" and scene.get("active_person") not in shown:
                raise ValueError("Speaker focus needs an explicit active person")
    if abs(previous - duration) > .001:
        raise ValueError("Framing does not cover the full clip")
    return copy.deepcopy(plan)


def manual_plan(clip, regions, policy="auto"):
    regions = validate_regions(regions)
    layout = choose_layout(regions, policy)
    plan = {"schema_version": 1, "clip_start": clip["start"], "clip_end": clip["end"],
            "policy": policy, "method": "manual-regions", "needs_review": False,
            "note": "Source regions reviewed manually.", "samples": [], "evidence": {},
            "scenes": [{"start": 0, "end": clip["end"] - clip["start"], "regions": regions,
                        "layout": layout, "active_person": None, "panels": panels_for(regions, layout),
                        "reason": "Reviewed source regions; protected content kept visible."}]}
    return validate_plan(plan, clip)
