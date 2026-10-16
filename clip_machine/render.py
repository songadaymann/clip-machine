"""Exact-time rendering and caption generation; no dependency on selection models."""
from __future__ import annotations

import copy
import math
import shutil
import tempfile
from pathlib import Path

from .captions import FONT_FILE, STYLE_VERSION, caption_anchors, caption_plan, write_caption_files
from .media import probe, run
from .framing import fallback_plan, validate_plan

FONTS = Path(__file__).parent / "fonts"
# Run from the stage folder: relative names avoid filtergraph escaping of user paths.
SUBTITLES = "ass=captions.ass:fontsdir=fonts"


def pixel_rect(rectangle, width, height):
    x, y, w, h = rectangle
    left = max(0, min(width - 2, int(x * width) // 2 * 2))
    top = max(0, min(height - 2, int(y * height) // 2 * 2))
    right = min(width // 2 * 2, math.ceil((x + w) * width / 2) * 2)
    bottom = min(height // 2 * 2, math.ceil((y + h) * height / 2) * 2)
    rw, rh = max(2, right - left), max(2, bottom - top)
    return left, top, rw, rh


def composition_filter(plan, media, burn_captions=False, still=False):
    """One decoded source, synchronized visual branches, one external audio map."""
    scenes = plan["scenes"][:1] if still else plan["scenes"]
    commands = []
    source = "0:%d" % media["video_stream"]
    count = len(scenes)
    commands.append("[%s]split=%d%s" % (source, count, "".join("[scene%d]" % i for i in range(count))))
    outputs = []
    for i, scene in enumerate(scenes):
        duration = 1 if still else scene["end"] - scene["start"]
        start, end = (0, 1) if still else (scene["start"], scene["end"])
        panels = scene["panels"]
        commands.append("[scene%d]trim=start=%s:end=%s,setpts=PTS-STARTPTS,split=%d%s" %
                        (i, start, end, len(panels), "".join("[s%dp%d]" % (i, n) for n in range(len(panels)))))
        commands.append("color=c=0x18211d:s=720x1280:r=30:d=%s[bg%d]" % (duration, i))
        previous = "bg%d" % i
        for n, panel in enumerate(panels):
            x, y, w, h = pixel_rect(panel["source"], media["width"], media["height"])
            dx, dy, dw, dh = [int(v) // 2 * 2 for v in panel["destination"]]
            commands.append("[s%dp%d]crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                            "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=0x18211d,setsar=1[p%dp%d]" %
                            (i, n, w, h, x, y, dw, dh, dw, dh, i, n))
            target = "layer%dp%d" % (i, n)
            commands.append("[%s][p%dp%d]overlay=%d:%d:shortest=1[%s]" % (previous, i, n, dx, dy, target))
            previous = target
        output = "composed%d" % i
        commands.append("[%s]fps=30,tpad=stop_mode=clone:stop_duration=0.1,trim=duration=%s,setpts=PTS-STARTPTS[%s]" %
                        (previous, duration, output))
        outputs.append("[" + output + "]")
    commands.append("%sconcat=n=%d:v=1:a=0,format=yuv420p%s[v]" %
                    ("".join(outputs), count, "," + SUBTITLES if burn_captions else ""))
    return ";".join(commands)


def framing_preview(source_image, plan, output):
    """A still proof of the same composition used for the actual export."""
    source_media = probe_image(source_image)
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-filter_complex_threads", "1", "-loop", "1",
         "-i", str(source_image), "-filter_complex", composition_filter(plan, source_media, still=True),
         "-map", "[v]", "-frames:v", "1", str(output)])


def probe_image(path):
    import json
    info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))["streams"][0]
    return {"width": info["width"], "height": info["height"], "video_stream": info["index"]}


def render_scene_sequence(source, media, clip, plan, stage):
    """Bound memory: render successive shots without buffering future split branches."""
    entries = []
    for i, scene in enumerate(plan["scenes"]):
        duration = scene["end"] - scene["start"]
        local = copy.deepcopy(scene)
        local.update(start=0, end=duration)
        shot = {**plan, "scenes": [local]}
        name = "scene-%03d.mp4" % i
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-filter_complex_threads", "1",
             "-ss", str(clip["start"] + scene["start"]), "-i", str(source), "-filter_complex",
             composition_filter(shot, media), "-map", "[v]", "-an", "-t", str(duration),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(stage / name)])
        entries.append("file '" + name + "'")
    manifest = stage / "scenes.txt"
    manifest.write_text("\n".join(entries) + "\n")
    return manifest


def render_clip(source, media, transcript, clip, folder, layout="original", burn_captions=True):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    portrait = layout == "portrait"
    video = media["has_video"] or portrait
    suffix = ".mp4" if video else ".m4a"
    basename = "%s-r%d-%s-%s" % (clip["id"], clip["revision"], layout, "captioned" if burn_captions else "plain")
    duration = clip["end"] - clip["start"]
    plan = None
    if portrait and media["has_video"]:
        plan = validate_plan(clip.get("framing") or fallback_plan(clip, "Framing not analyzed; preserve the full source."), clip)
    captions = caption_plan(transcript, clip, portrait)
    with tempfile.TemporaryDirectory(prefix="render-", dir=str(folder)) as temporary:
        stage = Path(temporary)
        write_caption_files(stage, captions, portrait, caption_anchors(plan, media, portrait, media["has_video"]))
        (stage / "fonts").mkdir()
        shutil.copyfile(str(FONTS / FONT_FILE), str(stage / "fonts" / FONT_FILE))
        command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-filter_complex_threads", "1", "-ss", str(clip["start"]), "-i", str(source)]
        audio_stream = "0:%d" % media["audio_stream"]
        video_stream = "0:%d" % media["video_stream"] if media["has_video"] else None
        if not media["has_video"] and portrait:
            wave = ("[" + audio_stream + "]asplit[a][w];[w]showwaves=s=640x180:mode=line:colors=0xd6ff45:rate=30,"
                    "format=yuv420p,pad=720:1280:40:500:color=0x18211d")
            if burn_captions:
                wave += "," + SUBTITLES
            command += ["-filter_complex", wave + "[v]", "-map", "[v]", "-map", "[a]"]
        elif media["has_video"]:
            if plan and len(plan["scenes"]) > 1:
                sequence = render_scene_sequence(source, media, clip, plan, stage)
                command += ["-f", "concat", "-safe", "0", "-i", str(sequence), "-map", "1:v:0", "-map", audio_stream]
                if burn_captions:
                    command += ["-vf", SUBTITLES]
            elif plan:
                command += ["-filter_complex", composition_filter(plan, media, burn_captions), "-map", "[v]", "-map", audio_stream]
            else:
                filters = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
                if burn_captions:
                    filters.append(SUBTITLES)
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
            "burn_captions": bool(video and burn_captions), "width": rendered["width"], "height": rendered["height"],
            "caption_style": STYLE_VERSION, "caption_mode": captions["mode"],
            "framing_method": plan["method"] if plan else None,
            "framing_layouts": list(dict.fromkeys(s["layout"] for s in plan["scenes"])) if plan else []}
