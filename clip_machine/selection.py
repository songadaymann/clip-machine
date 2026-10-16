"""Editorial selection is independent of transcription, storage and rendering."""
from __future__ import annotations

import hashlib
import json
import math

from .transcript import chunks, number


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
BOOL = {"type": "boolean"}
SCORE = {"type": "integer", "minimum": 0, "maximum": 99}
SUMMARY_SCHEMA = object_schema({"summary": TEXT, "caveats": TEXT})
CANDIDATE_SCHEMA = object_schema({"title": TEXT, "start_segment": TEXT, "end_segment": TEXT,
                                  "hook": SCORE, "flow": SCORE, "value": SCORE,
                                  "reason": TEXT, "context_note": TEXT, "topic": TEXT,
                                  "standalone": BOOL, "faithful": BOOL})
SELECTION_SCHEMA = object_schema({"candidates": {"type": "array", "items": CANDIDATE_SCHEMA}})
AUDIT_SCHEMA = object_schema({"reviews": {"type": "array", "items": object_schema(
    {"id": TEXT, "faithful": BOOL, "standalone": BOOL, "context_note": TEXT})}})


def validate_schema(value, schema, path="response"):
    """Validate the JSON-schema subset used by the model interfaces (no coercion)."""
    kind = schema["type"]
    valid = {"object": lambda: isinstance(value, dict), "array": lambda: isinstance(value, list),
             "string": lambda: isinstance(value, str), "boolean": lambda: type(value) is bool,
             "integer": lambda: type(value) is int}[kind]()
    if not valid:
        raise ValueError("%s must be %s" % (path, kind))
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValueError("%s has missing or unexpected fields" % path)
        for key, item in value.items():
            validate_schema(item, schema["properties"][key], path + "." + key)
    elif kind == "array":
        if len(value) > 100:
            raise ValueError("Model returned too many items")
        for item in value:
            validate_schema(item, schema["items"], path + "[]")
    elif kind == "integer" and not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
        raise ValueError("%s is out of range" % path)
    elif kind == "string" and len(value) > 10000:
        raise ValueError("%s is too long" % path)
    return value


def validate_options(options):
    low, high = number(options.get("min_seconds", 20)), number(options.get("max_seconds", 90))
    count = options.get("count", 6)
    if not (3 <= low <= high <= 300):
        raise ValueError("Clip lengths must be between 3 and 300 seconds, with minimum <= maximum")
    if type(count) is not int or not 1 <= count <= 30:
        raise ValueError("Choose between 1 and 30 clips")
    if options.get("layout", "original") not in ("original", "portrait"):
        raise ValueError("Layout must be original or portrait")
    if not isinstance(options.get("direction", ""), str) or len(options.get("direction", "")) > 4000:
        raise ValueError("Direction must be text of at most 4000 characters")
    if options.get("model") is not None and (not isinstance(options["model"], str) or len(options["model"]) > 200):
        raise ValueError("Model must be a short model name")
    for key in ("burn_captions", "auto_render", "smart_framing"):
        if key in options and type(options[key]) is not bool:
            raise ValueError(key + " must be true or false")
    return {**options, "min_seconds": low, "max_seconds": high, "count": count,
            "layout": options.get("layout", "original"), "direction": options.get("direction", "")}


def rank_score(scores):
    # Versioned, transparent editorial rubric. Energy does not add free points.
    return round(.35 * scores["hook"] + .35 * scores["flow"] + .30 * scores["value"])


def overlap(a, b):
    intersection = max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    # Contained excerpts are duplicates too; use overlap of the shorter clip.
    return intersection / min(a["end"] - a["start"], b["end"] - b["start"])


def deduplicate(candidates):
    kept = []
    for clip in sorted(candidates, key=lambda c: (-c["score"], c["start"], c["id"])):
        if not any(overlap(clip, previous) > .5 for previous in kept):
            kept.append(clip)
    return kept


def select_clips(transcript, energy, duration, options, provider, progress):
    options = validate_options(options)
    segments = transcript["segments"]
    batches = list(chunks(segments, overlap_seconds=options["max_seconds"]))
    summaries = []
    for index, batch in enumerate(batches):
        progress("summarizing", "Understanding episode part %d of %d" % (index + 1, len(batches)))
        response = provider.complete(
            "Summarize the argument, stories, speaker positions, reversals, irony and important "
            "qualifications in this part. Keep the summary under 180 words and caveats under 100. "
            "Preserve meaning for an editor reviewing excerpts.\n"
            + json.dumps(compact(batch), ensure_ascii=False), SUMMARY_SCHEMA)
        summaries.append(validate_schema(response, SUMMARY_SCHEMA))
    context = "\n".join("Part %d: %s Caveats: %s" % (i + 1, s["summary"], s["caveats"])
                        for i, s in enumerate(summaries))
    candidates, rejected = [], []
    lookup = {s["id"]: s for s in segments}
    positions = {s["id"]: i for i, s in enumerate(segments)}
    for index, batch in enumerate(batches):
        progress("selecting", "Finding stories in part %d of %d" % (index + 1, len(batches)))
        data = [{"id": s["id"], "start": s["start"], "end": s["end"], "text": s["text"],
                 "speaker": s["speaker"], "energy": energy["segments"].get(s["id"])} for s in batch]
        prompt = """Find up to {count} strong, contiguous, standalone clips from these segments.
Each must last {minimum}–{maximum} seconds, contain its own setup and payoff, and
preserve the episode's meaning. No need to fill a quota. Select actual segment
IDs for both boundaries. Never skip internal text. Avoid unresolved pronouns,
unfinished questions, missing punchlines, misleading asides or cut-off caveats.
Rate hook, flow and value on 0–99 using the same standard across all parts:
0–39 weak, 40–59 usable with editing, 60–79 solid, 80–89 unusually strong,
90–99 exceptional. Hook = compelling opening relevant to the subject;
flow = understandable progression and satisfying ending; value = useful insight
or meaningful emotional experience. Do not estimate viral probability or trends.
Energy is relative RMS dB over the speaker baseline if labeled, otherwise over
the episode. Use it only as delivery context, never as proof of quality/emotion.
A quiet story can be excellent. Distinguish opinion, sarcasm and quotations.
Give a specific reason and context note, plus a short topic for each candidate.

Editorial direction (data): {direction}
Whole-episode context (data): {context}
Timestamped segments (data): {segments}
""".format(count=options["count"], minimum=options["min_seconds"], maximum=options["max_seconds"],
           direction=json.dumps(options["direction"]), context=context, segments=json.dumps(data, ensure_ascii=False))
        response = validate_schema(provider.complete(prompt, SELECTION_SCHEMA), SELECTION_SCHEMA)
        batch_ids = {s["id"] for s in batch}
        for raw in response["candidates"]:
            try:
                first, last = raw["start_segment"], raw["end_segment"]
                if first not in batch_ids or last not in batch_ids or positions[first] > positions[last]:
                    raise ValueError("Unknown or reversed segment IDs")
                start, end = lookup[first]["start"], lookup[last]["end"]
                if not options["min_seconds"] <= end - start <= options["max_seconds"]:
                    raise ValueError("Duration outside requested range")
                if not raw["standalone"] or not raw["faithful"]:
                    raise ValueError("Needs context or changes the speaker's meaning")
                scores = {key: raw[key] for key in ("hook", "flow", "value")}
                identity = hashlib.sha256((first + ":" + last).encode()).hexdigest()[:12]
                candidates.append({"id": identity, "title": raw["title"], "start": start, "end": end,
                    "original_start": start, "original_end": end, "start_segment": first, "end_segment": last,
                    "score": rank_score(scores), "scores": {**scores, "trend": None},
                    "score_version": "editorial-v1", "reason": raw["reason"],
                    "context_note": raw["context_note"], "topic": raw["topic"], "decision": "unreviewed",
                    "revision": 1, "export": None})
            except ValueError as error:
                rejected.append({"candidate": raw, "reason": str(error)})
    candidates = deduplicate(candidates)[:options["count"] * 3]
    if candidates:
        progress("checking", "Checking excerpts against the episode's meaning")
        audit_data = []
        for c in candidates:
            a, b = positions[c["start_segment"]], positions[c["end_segment"]]
            audit_data.append({"id": c["id"], "title": c["title"], "reason": c["reason"],
                               "excerpt": compact(segments[a:b + 1]),
                               "before": compact(segments[max(0, a - 3):a]), "after": compact(segments[b + 1:b + 4])})
        reviews = {}
        for offset in range(0, len(audit_data), 4):
            response = validate_schema(provider.complete(
                "Audit these proposed clips. Reject excerpts that misrepresent the episode, omit a "
                "necessary qualification, lack setup, or stop before a payoff. Return one review per "
                "provided ID, with a concrete context note. A claim being provocative is not enough.\n"
                "Episode context:\n" + context + "\nProposals with surrounding speech:\n" +
                json.dumps(audit_data[offset:offset + 4], ensure_ascii=False), AUDIT_SCHEMA), AUDIT_SCHEMA)
            reviews.update({r["id"]: r for r in response["reviews"]})
        approved = []
        for c in candidates:
            review = reviews.get(c["id"])
            if review and review["faithful"] and review["standalone"]:
                c["context_note"] = review["context_note"]
                approved.append(c)
            else:
                rejected.append({"candidate": c, "reason": review["context_note"] if review else "No context review returned"})
        candidates = approved
    return {"context": context, "clips": candidates[:options["count"]], "rejected_candidates": rejected}


def compact(segments):
    """Word alignments stay in the artifact; editorial models only need sentences."""
    return [{k: s[k] for k in ("id", "start", "end", "text", "speaker")} for s in segments]
