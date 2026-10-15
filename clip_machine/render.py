"""Exact-time rendering and caption generation; no dependency on selection models."""
from __future__ import annotations

import html
import shutil
import tempfile
from pathlib import Path

from .media import probe, run


def timestamp(seconds, separator=","):
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    secs, millis = divmod(millis, 1000)
    return "%02d:%02d:%02d%s%03d" % (hours, minutes, secs, separator, millis)


def caption_cues(segments, start, end):
    cues = []
    for segment in segments:
        if segment["end"] <= start or segment["start"] >= end:
            continue
        if not segment["words"]:
            cues.append({"start": max(0, segment["start"] - start),
                         "end": min(end, segment["end"]) - start, "text": segment["text"]})
            continue
        group = []
        for word in segment["words"]:
            if word["end"] <= start or word["start"] >= end:
                continue
            group.append(word)
            if len(group) >= 6 or word["end"] - group[0]["start"] >= 2.5:
                cues.append(_cue(group, start, end))
                group = []
        if group:
            cues.append(_cue(group, start, end))
    return [c for c in cues if c["end"] > c["start"] and c["text"].strip()]


def _cue(group, start, end):
    return {"start": max(0, group[0]["start"] - start),
            "end": min(end, group[-1]["end"]) - start,
            "text": " ".join(w["text"] for w in group)}


def ass_time(seconds):
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return "%d:%02d:%02d.%02d" % (hours, minutes, secs, centis)


def write_captions(folder, cues, portrait=False):
    srt, vtt = [], ["WEBVTT\n"]
    for i, cue in enumerate(cues, 1):
        # Do not let transcript markup become subtitle formatting.
        text = html.escape(cue["text"].replace("\r", " ").replace("\n", " "))
        srt.append("%d\n%s --> %s\n%s\n" % (i, timestamp(cue["start"]), timestamp(cue["end"]), text))
        vtt.append("%s --> %s\n%s\n" % (timestamp(cue["start"], "."), timestamp(cue["end"], "."), text))
    (folder / "captions.srt").write_text("\n".join(srt))
    (folder / "captions.vtt").write_text("\n".join(vtt))
    width, height = (720, 1280) if portrait else (1280, 720)
    font_size, margin = (40, 230) if portrait else (38, 54)
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""".format(width=width, height=height, font_size=font_size, margin=margin)
    for cue in cues:
        text = cue["text"].replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\r", " ").replace("\n", " ")
        header += "Dialogue: 0,%s,%s,Default,,0,0,0,,%s\n" % (ass_time(cue["start"]), ass_time(cue["end"]), text)
    (folder / "captions.ass").write_text(header)


def render_clip(source, media, transcript, clip, folder, layout="original", burn_captions=True):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    portrait = layout == "portrait"
    video = media["has_video"] or portrait
    suffix = ".mp4" if video else ".m4a"
    basename = "%s-r%d-%s-%s" % (clip["id"], clip["revision"], layout, "captioned" if burn_captions else "plain")
    cues = caption_cues(transcript["segments"], clip["start"], clip["end"])
    duration = clip["end"] - clip["start"]
    with tempfile.TemporaryDirectory(prefix="render-", dir=str(folder)) as temporary:
        stage = Path(temporary)
        write_captions(stage, cues, portrait)
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", str(clip["start"]), "-i", str(source)]
        audio_stream = "0:%d" % media["audio_stream"]
        video_stream = "0:%d" % media["video_stream"] if media["has_video"] else None
        if not media["has_video"] and portrait:
            wave = ("[" + audio_stream + "]asplit[a][w];[w]showwaves=s=640x180:mode=line:colors=0xd6ff45:rate=30,"
                    "format=yuv420p,pad=720:1280:40:500:color=0x18211d")
            if burn_captions:
                wave += ",ass=captions.ass"
            command += ["-filter_complex", wave + "[v]", "-map", "[v]", "-map", "[a]"]
        elif media["has_video"]:
            filters = (["scale=720:1280:force_original_aspect_ratio=increase", "crop=720:1280", "setsar=1"]
                       if portrait else ["scale=trunc(iw/2)*2:trunc(ih/2)*2"])
            if burn_captions:
                filters.append("ass=captions.ass")
            command += ["-map", video_stream, "-map", audio_stream, "-vf", ",".join(filters)]
        else:
            command += ["-vn", "-map", audio_stream]
        if video:
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
        command += ["-t", str(duration), "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1",
                    "-movflags", "+faststart", str(stage / ("clip" + suffix))]
        run(command, cwd=str(stage))
        rendered = probe(stage / ("clip" + suffix))
        if abs(rendered["duration"] - duration) > .25:
            raise RuntimeError("Export duration differs from the selected boundaries")
        names = {}
        for key, filename, extension in [("media", "clip" + suffix, suffix), ("srt", "captions.srt", ".srt"),
                                          ("vtt", "captions.vtt", ".vtt"), ("ass", "captions.ass", ".ass")]:
            target = folder / (basename + extension)
            shutil.move(str(stage / filename), str(target))
            names[key] = "exports/" + target.name
    return {**names, "duration": rendered["duration"], "layout": layout, "revision": clip["revision"],
            "burn_captions": bool(video and burn_captions), "width": rendered["width"], "height": rendered["height"]}
