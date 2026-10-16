"""Caption display plans: deletion-only cleanup, phrasing, emphasis, placement, files.

Captions reference transcript words by `segment_id:index`. The transcript and
audio are never changed; suggestions and manual overrides are stored separately.
"""
from __future__ import annotations

import html
import json
import math
import re

from .selection import object_schema, validate_schema
from .storage import now

RULES_VERSION = "clean-v1"
STYLE_VERSION = "pop-v1"
EMPHASIS_PROMPT_VERSION = "caption-emphasis-v1"
FONT_NAME = "Archivo Condensed Black"
FONT_FILE = "ArchivoCondensed-Black.ttf"
ACCENT = "&H0C02F6&"  # #F6020C, the Anti-AI AI Club logo red, in ASS blue-green-red order
WHITE = "&HFFFFFF&"
EMPHASIS_SPACING = 3.0  # Seconds between model-suggested emphasis words
HOOK_SECONDS = 2.5
# Approximate areas that TikTok, Reels and Shorts cover with their own UI, on 720×1280.
SAFE_TOP, SAFE_BOTTOM = 170, 1010
FILLERS = {"um", "uh", "uhm", "umm", "uhh", "erm", "er", "ah", "hmm", "mm", "mhm"}
ASIDES = (("you", "know"), ("i", "mean"), ("like",))
STUTTERS = {"i", "the", "a", "and", "but", "so", "we", "you", "it", "that", "they", "he", "she", "to", "in", "of"}
WEAK_ENDINGS = {"a", "an", "the", "to", "of", "and", "or", "but", "in", "on", "for", "with", "my", "our", "your",
                "as", "at", "from", "by"}
LEAD_INS = WEAK_ENDINGS | {"so", "that", "because", "if", "then"}
KEEP_TOGETHER = {("a", "lot"), ("lot", "of"), ("kind", "of"), ("sort", "of"), ("a", "little"), ("little", "bit"),
                 ("bit", "of"), ("going", "to"), ("have", "to"), ("want", "to"), ("i", "don't"), ("don't", "know"),
                 ("i", "think"), ("don't", "think"), ("you", "know"), ("i", "mean"), ("a", "bit"), ("as", "well")}
# Advance widths of the bundled font (units per em 1000). libass sizes text by the
# Windows cell height (ascent 1100 + descent 410), so 78px text has a 51.7px em.
ADVANCES = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '’?!-&%$#@\"/().,", [
    615, 594, 604, 601, 551, 493, 642, 622, 299, 526, 621, 512, 796, 626, 649, 566, 649, 601, 556, 560, 616,
    589, 822, 595, 583, 560, 520, 472, 509, 518, 514, 516, 521, 489, 512, 521, 134, 235, 229, 488, 289, 272,
    677, 830, 472, 554, 798, 431, 288, 389, 389, 260, 260]))
CELL_HEIGHT = 1510
SPACING = 1.5
WORD_REF = re.compile(r"s\d{6}:\d{1,5}")
EMPHASIS_SCHEMA = object_schema({"clips": {"type": "array", "items": object_schema(
    {"id": {"type": "string"}, "words": {"type": "array", "items": {"type": "string"}}})}})


def bare(text):
    return re.sub(r"[^\w']", "", text).lower()


def boundary(text):
    return bool(re.search(r"(\.\.\.|…|[.!?,;:—-])[\"'”’)]*$", text))


def trailing_off(text):
    return bool(re.search(r"(\.\.\.|…|[—-])$", text))


def episode_words(transcript):
    """All words in order. Segment-only imports become untimed tokens of that segment."""
    words = []
    for segment in transcript["segments"]:
        timed = bool(segment["words"])
        tokens = segment["words"] if timed else [{"text": t} for t in segment["text"].split()]
        for index, token in enumerate(tokens):
            if token["text"].strip():
                words.append({"ref": "%s:%d" % (segment["id"], index), "text": token["text"].strip(),
                              "start": token["start"] if timed else None, "end": token["end"] if timed else None,
                              "segment": segment})
    return words


def suggest_hidden(words):
    """Deletion-only cleanup: filler, false starts and set-off asides. Returns {ref: reason}."""
    hidden = {}
    keys = [bare(w["text"]) for w in words]
    for i, word in enumerate(words):
        if keys[i] in FILLERS:
            hidden[word["ref"]] = "filler"
    visible = [i for i in range(len(words)) if words[i]["ref"] not in hidden]
    for n, i in enumerate(visible):
        text = words[i]["text"]
        stutter = text.endswith(",") and keys[i] in STUTTERS
        if not (trailing_off(text) or stutter) or n + 1 >= len(visible):
            continue
        after = [keys[j] for j in visible[n + 1:n + 6]]
        # The fragment is everything since the previous pause or sentence end (at most five words).
        first = n
        while first > 0 and n - first < 4 and not boundary(words[visible[first - 1]]["text"]):
            first -= 1
        fragment = [keys[j] for j in visible[first:n + 1]]
        for k in range(1 if stutter else len(fragment), 0, -1):
            if fragment[-k:] == after[:k]:
                hidden.update({words[j]["ref"]: "restart" for j in visible[n - k + 1:n + 1]})
                break
        else:
            # "There... There's", "There is... There's": a short fragment restarted as a contraction.
            if not stutter and len(fragment) <= 2 and fragment[0] and after[0] != fragment[0] and after[0].startswith(fragment[0]):
                hidden.update({words[j]["ref"]: "restart" for j in visible[first:n + 1]})
    visible = [i for i in range(len(words)) if words[i]["ref"] not in hidden]
    for n, i in enumerate(visible):
        for phrase in ASIDES:
            span = visible[n:n + len(phrase)]
            if len(span) < len(phrase) or tuple(keys[j] for j in span) != phrase:
                continue
            before = n == 0 or boundary(words[visible[n - 1]]["text"])
            if before and boundary(words[span[-1]]["text"]):
                hidden.update({words[j]["ref"]: "aside" for j in span})
    return hidden


def caption_settings(clip):
    saved = clip.get("captions") or {}
    edits = saved.get("edits") or {}
    hook = saved.get("hook") or {}
    return {"mode": saved.get("mode", "clean"),
            "edits": {"hidden": dict(edits.get("hidden") or {}), "emphasis": dict(edits.get("emphasis") or {})},
            "emphasis_suggestion": saved.get("emphasis_suggestion"),
            "hook": {"enabled": bool(hook.get("enabled")), "text": hook.get("text")}}


def caption_plan(transcript, clip, portrait=True):
    """Words, visibility, emphasis and phrase chunks for the clip's current trim (clip-relative times)."""
    settings = caption_settings(clip)
    everything = episode_words(transcript)
    rules = suggest_hidden(everything)
    start, end = clip["start"], clip["end"]
    suggested = set((settings["emphasis_suggestion"] or {}).get("refs", []))
    words = []
    for word in everything:
        if word["start"] is None:
            segment = word["segment"]
            if segment["end"] <= start or segment["start"] >= end:
                continue
        elif word["end"] <= start or word["start"] >= end:
            continue
        ref = word["ref"]
        manual_hidden = settings["edits"]["hidden"].get(ref)
        manual_emphasis = settings["edits"]["emphasis"].get(ref)
        hidden = settings["mode"] == "clean" and (rules.get(ref) is not None if manual_hidden is None else manual_hidden)
        emphasis = not hidden and (ref in suggested if manual_emphasis is None else manual_emphasis)
        timed = word["start"] is not None
        words.append({"ref": ref, "text": word["text"], "segment_id": word["segment"]["id"],
                      "start": max(0, word["start"] - start) if timed else None,
                      "end": min(end, word["end"]) - start if timed else None,
                      "segment_start": max(0, word["segment"]["start"] - start),
                      "segment_end": min(end, word["segment"]["end"]) - start,
                      "rule": rules.get(ref), "suggested_emphasis": ref in suggested,
                      "manual_hidden": manual_hidden, "manual_emphasis": manual_emphasis,
                      "hidden": hidden, "emphasis": emphasis})
    timed = all(w["start"] is not None for w in words)
    visible = [w for w in words if not w["hidden"]]
    for w in visible:
        w["trailing"] = trailing_off(w["text"])
        if settings["mode"] == "clean":
            w["text"] = w["text"].replace("...", "").replace("…", "") or w["text"]
    size, max_width = caption_size(portrait, timed), 580 if portrait else 1040
    chunks = chunk_words(visible, size, max_width) if timed else segment_chunks(visible)
    starts = {c["refs"][0] for c in chunks}
    for w in words:
        w["chunk_start"] = w["ref"] in starts
    return {"timing": "word" if timed else "segment", "mode": settings["mode"], "rules_version": RULES_VERSION,
            "style_version": STYLE_VERSION, "duration": end - start,
            "hook": {"enabled": settings["hook"]["enabled"], "text": settings["hook"]["text"] or clip.get("title", "")},
            "emphasis_suggestion": settings["emphasis_suggestion"], "words": words, "chunks": chunks}


def display(text):
    return text.upper().rstrip(".,;:")


def line_width(words, size):
    """Rendered width in canvas pixels, with emphasized words at 120%."""
    total = 0
    for word in words:
        text = display(word["text"])
        width = sum(ADVANCES.get(c, 594) for c in text) * size / CELL_HEIGHT + SPACING * len(text)
        total += width * (1.2 if word.get("emphasis") else 1)
    return total + (len(words) - 1) * (ADVANCES[" "] * size / CELL_HEIGHT + SPACING)


def chunk_words(words, size, max_width, max_words=4, pause=.3, longest=2.5):
    """Balanced 1–4 word phrases. Hard breaks at sentence ends, trailing-off and pauses."""
    runs, run = [], []
    for i, word in enumerate(words):
        run.append(word)
        following = words[i + 1] if i + 1 < len(words) else None
        if (following is None or word.get("trailing") or re.search(r"[.!?]$", word["text"])
                or following["start"] - word["end"] > pause):
            runs.append(run)
            run = []
    # A lone "but" or "so" before a pause joins the phrase it introduces.
    merged = []
    for run in runs:
        if merged and all(bare(w["text"]) in LEAD_INS for w in merged[-1]) and run[0]["start"] - merged[-1][-1]["end"] < 1.5:
            merged[-1] = merged[-1] + run
        else:
            merged.append(run)
    groups = [group for run in merged for group in balanced(run, size, max_width, max_words, longest)]
    chunks = []
    for n, group in enumerate(groups):
        following = groups[n + 1][0]["start"] if n + 1 < len(groups) else math.inf
        finish = min(group[-1]["end"] + .4, following)
        chunks.append({"start": group[0]["start"], "end": max(finish, group[0]["start"] + .2),
                       "refs": [w["ref"] for w in group]})
    return chunks


def balanced(run, size, max_width, max_words, longest):
    """Fewest lines, then the most even line widths; avoid ending a line on a weak word."""
    best = [(0, [])] + [None] * len(run)
    for end in range(1, len(run) + 1):
        for count in range(1, min(max_words, end) + 1):
            group = run[end - count:end]
            width = line_width(group, size)
            if count > 1 and (width > max_width or group[-1]["end"] - group[0]["start"] > longest):
                continue
            previous = best[end - count]
            if previous is None:
                continue
            last = group[-1]["text"]
            split = end < len(run) and (bare(last), bare(run[end]["text"])) in KEEP_TOGETHER
            # Splitting "a lot | of" reads worse than one more caption line.
            cost = (previous[0] + 1000 + (max(0, max_width - width) / 30) ** 2 + (1500 if split else 0)
                    + (300 if bare(last) in WEAK_ENDINGS and end < len(run) else 0)
                    - (200 if last.endswith(",") else 0))
            if best[end] is None or cost < best[end][0]:
                best[end] = (cost, previous[1] + [group])
    return best[-1][1]


def segment_chunks(words):
    """Segment-only transcripts cannot time phrases; keep one cue per segment."""
    chunks = []
    for word in words:
        if chunks and chunks[-1]["segment_id"] == word["segment_id"]:
            chunks[-1]["refs"].append(word["ref"])
        else:
            chunks.append({"segment_id": word["segment_id"], "start": word["segment_start"],
                           "end": word["segment_end"], "refs": [word["ref"]]})
    return [{k: c[k] for k in ("start", "end", "refs")} for c in chunks if c["end"] > c["start"]]


def sidecar_cues(plan, max_chars=42, pause=.8, longest=5):
    """Readable SRT/VTT cues from the same visible words, in normal case."""
    lookup = {w["ref"]: w for w in plan["words"]}
    if plan["timing"] == "segment":
        return [{"start": c["start"], "end": c["end"], "text": " ".join(lookup[r]["text"] for r in c["refs"])}
                for c in plan["chunks"]]
    visible = [lookup[r] for c in plan["chunks"] for r in c["refs"]]
    cues, group = [], []
    for i, word in enumerate(visible):
        group.append(word)
        following = visible[i + 1] if i + 1 < len(visible) else None
        text = " ".join(w["text"] for w in group)
        if (following is None or re.search(r"[.!?]$", word["text"]) or following["start"] - word["end"] > pause
                or len(text) + 1 + len(following["text"]) > max_chars or following["end"] - group[0]["start"] > longest):
            cues.append({"start": group[0]["start"], "end": word["end"], "text": text})
            group = []
    return [c for c in cues if c["end"] > c["start"]]


def subtitle_text(text):
    # Do not let transcript markup become subtitle formatting.
    return html.escape(text.replace("\r", " ").replace("\n", " "))


def ass_text(text):
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", " ").replace("\n", " ")


def timestamp(seconds, separator=","):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return "%02d:%02d:%02d%s%03d" % (hours, minutes, secs, separator, millis)


def ass_time(seconds):
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return "%d:%02d:%02d.%02d" % (hours, minutes, secs, centis)


def content_boxes(scene, media):
    """Where each panel's image actually lands (contain fit), with its kind and any protected subject."""
    regions = {r["id"]: r for r in scene.get("regions", [])}
    boxes = []
    for panel in scene["panels"]:
        dx, dy, dw, dh = panel["destination"]
        sx, sy, sw, sh = panel["source"]
        width, height = sw * media["width"], sh * media["height"]
        scale = min(dw / width, dh / height)
        fw, fh = width * scale, height * scale
        x0, y0 = dx + (dw - fw) / 2, dy + (dh - fh) / 2
        region = regions.get(panel["region_id"])
        box = {"kind": region["kind"] if region else "content", "box": (x0, y0, fw, fh), "subject": None}
        if region and region.get("subject"):
            ux, uy, uw, uh = region["subject"]
            box["subject"] = (x0 + (ux - sx) / sw * fw, y0 + (uy - sy) / sh * fh, uw / sw * fw, uh / sh * fh)
        boxes.append(box)
    return boxes


def band_cost(center, boxes, band):
    top, bottom = center - band / 2, center + band / 2

    def covered(box, weight):
        x, y, w, h = box
        return weight * w * max(0, min(bottom, y + h) - max(top, y))
    cost = 0
    for item in boxes:
        x, y, w, h = item["box"]
        if item["kind"] != "person":
            cost += covered(item["box"], .6)
        elif item["subject"]:
            cost += covered(item["subject"], 4) + covered(item["box"], .35)
        else:
            # Without a tracked face, assume heads sit in the upper part of a camera panel.
            cost += covered((x, y, w, h * .6), 1) + covered((x, y + h * .6, w, h * .4), .35)
    return cost


def caption_anchor(boxes, band=120, preferred=.62 * 1280):
    candidates = range(int(SAFE_TOP + band / 2), int(SAFE_BOTTOM - band / 2) + 1, 4)
    return min(candidates, key=lambda y: (band_cost(y, boxes, band) + 20 * abs(y - preferred), y))


def caption_anchors(framing, media, portrait, has_video):
    """Clip-relative (start, end, y) caption centers. Captions avoid faces and game/content."""
    if not portrait:
        return [(0, math.inf, 640)]
    if not has_video:
        return [(0, math.inf, caption_anchor([{"kind": "content", "box": (40, 500, 640, 180), "subject": None}]))]
    return [(scene["start"], scene["end"], caption_anchor(content_boxes(scene, media))) for scene in framing["scenes"]]


def write_caption_files(folder, plan, portrait=False, anchors=None):
    cues = sidecar_cues(plan)
    srt, vtt = [], ["WEBVTT\n"]
    for i, cue in enumerate(cues, 1):
        text = subtitle_text(cue["text"])
        srt.append("%d\n%s --> %s\n%s\n" % (i, timestamp(cue["start"]), timestamp(cue["end"]), text))
        vtt.append("%s --> %s\n%s\n" % (timestamp(cue["start"], "."), timestamp(cue["end"], "."), text))
    (folder / "captions.srt").write_text("\n".join(srt))
    (folder / "captions.vtt").write_text("\n".join(vtt))
    (folder / "captions.ass").write_text(ass_document(plan, portrait, anchors or [(0, math.inf, 640 if not portrait else 790)]))


def caption_size(portrait, timed):
    return (76 if timed else 60) if portrait else (56 if timed else 46)


def ass_document(plan, portrait, anchors):
    width, height = (720, 1280) if portrait else (1280, 720)
    timed = plan["timing"] == "word"
    size = caption_size(portrait, timed)
    hook_size, hook_y = (60, SAFE_TOP) if portrait else (44, 40)
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: %d" % width, "PlayResY: %d" % height,
             "WrapStyle: 0", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: Caption,%s,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,%s,0,1,6,3,5,40,40,0,1"
             % (FONT_NAME, size, SPACING),
             # An opaque box (border style 3) is filled with the outline colour.
             "Style: Hook,%s,%d,&H00FFFFFF,&H00FFFFFF,&H000C02F6,&H000C02F6,0,0,0,0,100,100,1,0,3,14,0,8,60,60,0,1"
             % (FONT_NAME, hook_size), "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    hook = plan["hook"]
    if hook["enabled"] and hook.get("text"):
        lines.append("Dialogue: 1,%s,%s,Hook,,0,0,0,,{\\an8\\pos(%d,%d)}%s" % (
            ass_time(0), ass_time(min(HOOK_SECONDS, plan["duration"])), width // 2, hook_y, ass_text(hook["text"].upper())))
    lookup = {w["ref"]: w for w in plan["words"]}

    def anchor(at):
        return next((y for start, end, y in anchors if start <= at < end), anchors[-1][2])
    for chunk in plan["chunks"]:
        group = [lookup[r] for r in chunk["refs"]]
        steps = range(len(group)) if timed else [None]
        for k in steps:
            start = chunk["start"] if not k else group[k]["start"]
            end = group[k + 1]["start"] if timed and k + 1 < len(group) else chunk["end"]
            if end <= start:
                continue
            parts = []
            for m, word in enumerate(group):
                scale = 120 if word["emphasis"] else 100
                red = word["emphasis"] and (not timed or m <= k) or (timed and m == k)
                pop = "\\fscx%d\\fscy%d\\t(0,90,\\fscx%d\\fscy%d)" % (scale * .8, scale * .8, scale, scale)
                tags = pop if timed and k == 0 else "\\fscx%d\\fscy%d" % (scale, scale)
                parts.append("{\\c%s%s}%s" % (ACCENT if red else WHITE, tags, ass_text(display(word["text"]))))
            lines.append("Dialogue: 0,%s,%s,Caption,,0,0,0,,{\\an5\\pos(%d,%d)}%s" % (
                ass_time(start), ass_time(end), width // 2, anchor(start), " ".join(parts)))
    return "\n".join(lines) + "\n"


def emphasis_candidates(transcript, clip):
    """Visible clean-mode words offered to the model, with short clip-local IDs."""
    plan = caption_plan(transcript, {**clip, "captions": {"mode": "clean"}})
    return [w for w in plan["words"] if not w["hidden"]], plan["timing"]


def suggest_emphasis(transcript, clips, provider):
    """Ask the provider for payoff words. Never raises: failures are recorded per clip."""
    requests = []
    for clip in clips:
        words, timing = emphasis_candidates(transcript, clip)
        local = {"w%d" % i: w for i, w in enumerate(words)}
        requests.append((clip, local, timing))
    results = {}
    for offset in range(0, len(requests), 4):
        batch = requests[offset:offset + 4]
        data = [{"id": clip["id"], "title": clip["title"],
                 "words": [{"id": key, "text": w["text"], "t": None if w["start"] is None else round(w["start"], 2)}
                           for key, w in local.items()]} for clip, local, _ in batch]
        try:
            response = validate_schema(provider.complete(
                "Choose caption emphasis for short-form podcast clips. For each clip, list the words that carry "
                "its payoff, contrast or surprise: key nouns, names, numbers or the decisive verb. Emphasis must "
                "not change meaning; skip filler, pronouns and function words. Give at most one word per three "
                "seconds of speech, most important first. Use only the supplied word IDs.\n"
                "Clips (data): " + json.dumps(data, ensure_ascii=False), EMPHASIS_SCHEMA), EMPHASIS_SCHEMA)
            chosen = {item["id"]: item["words"] for item in response["clips"]}
            error = None
        except Exception as failure:  # A styling suggestion must never fail the analysis.
            # CLI errors end with the cause, after echoed input; keep both ends.
            text = str(failure)
            chosen, error = {}, text if len(text) <= 500 else text[:120] + " … " + text[-360:]
        for clip, local, timing in batch:
            refs, times = [], []
            for key in chosen.get(clip["id"], []):
                word = local.get(key)
                if word is None or word["ref"] in refs:
                    continue
                at = word["start"] if word["start"] is not None else word["segment_start"]
                if any(abs(at - t) < EMPHASIS_SPACING for t in times):
                    continue
                refs.append(word["ref"])
                times.append(at)
            results[clip["id"]] = {"refs": refs, "source": provider.name, "model": getattr(provider, "model", None),
                                   "prompt_version": EMPHASIS_PROMPT_VERSION, "at": now(), "error": error}
    return results


def validate_caption_edit(changes, known_refs):
    """Check a manual caption edit. Overrides are {ref: true|false|null}; null clears an override."""
    if not isinstance(changes, dict) or set(changes) - {"mode", "hidden", "emphasis", "hook"}:
        raise ValueError("Invalid caption edit")
    if "mode" in changes and changes["mode"] not in ("clean", "verbatim"):
        raise ValueError("Caption mode must be clean or verbatim")
    for key in ("hidden", "emphasis"):
        values = changes.get(key, {})
        if not isinstance(values, dict) or len(values) > 2000:
            raise ValueError("Caption word overrides must be an object")
        for ref, value in values.items():
            if not WORD_REF.fullmatch(ref) or ref not in known_refs:
                raise ValueError("Unknown caption word")
            if value is not None and type(value) is not bool:
                raise ValueError("Caption overrides must be true, false or null")
    if "hook" in changes:
        hook = changes["hook"]
        if not isinstance(hook, dict) or set(hook) - {"enabled", "text"}:
            raise ValueError("Invalid hook title")
        if "enabled" in hook and type(hook["enabled"]) is not bool:
            raise ValueError("Hook enabled must be true or false")
        if hook.get("text") is not None and (not isinstance(hook["text"], str) or len(hook["text"]) > 120):
            raise ValueError("Hook titles must be text of at most 120 characters")
    return changes
