"""Local anonymous face observations and conservative speaker-activity evidence.

This baseline is audio-gated mouth motion, not a trained audiovisual speaker
classifier. Uncertain/cross-talk observations never justify hiding a participant.
"""
from __future__ import annotations

import math
import wave


def union_rect(boxes):
    x, y = min(b[0] for b in boxes), min(b[1] for b in boxes)
    right, bottom = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
    return [x, y, right - x, bottom - y]


def face_window(region, subjects, destination, media):
    """Choose a head-safe crop, or contain the full camera feed if it won't fit."""
    if not subjects:
        return region["rect"]
    protected = union_rect(subjects)
    parent = region["rect"]
    sx, sy = media["width"], media["height"]
    px, py, pw, ph = parent[0] * sx, parent[1] * sy, parent[2] * sx, parent[3] * sy
    x, y, w, h = protected[0] * sx, protected[1] * sy, protected[2] * sx, protected[3] * sy
    aspect = destination[2] / destination[3]
    crop_w = max(w * 1.5, h * 1.55 * aspect)
    crop_h = crop_w / aspect
    max_w = min(pw, ph * aspect)
    if max_w < w or max_w / aspect < h:
        return parent
    crop_w = min(crop_w, max_w)
    crop_h = crop_w / aspect
    cx, cy = x + w / 2, y + h / 2
    left = max(px, min(px + pw - crop_w, cx - crop_w / 2))
    top = max(py, min(py + ph - crop_h, cy - crop_h / 2))
    # Union of observations is the protected area, not only the first detection.
    if left > x or top > y or left + crop_w < x + w - .01 or top + crop_h < y + h - .01:
        return parent
    return [left / sx, top / sy, crop_w / sx, crop_h / sy]


def stable_speakers(observations, person_ids, minimum_hold=3):
    """Select only sustained, strong evidence; ambiguous windows retain the group."""
    timeline = []
    candidate, candidate_since = None, 0
    for observation in observations:
        t = observation["time"]
        scores = observation.get("mouth_activity", {})
        ranked = sorted(((scores.get(p, 0), p) for p in person_ids), reverse=True)
        active = None
        if observation.get("speech") and len(ranked) >= 2:
            best, person = ranked[0]
            second = ranked[1][0]
            if best >= 1.5 and best >= max(.8, second) * 1.8:
                active = person
        if active != candidate:
            candidate, candidate_since = active, t
        focused = active if active and t - candidate_since >= minimum_hold else None
        timeline.append({"time": t, "person_id": focused,
                         "evidence": "sustained audio-gated mouth motion" if focused else "uncertain; retain group"})
    # Short bursts at the tail of a long evidence run still shouldn't make a cut.
    start = 0
    for i in range(1, len(timeline) + 1):
        if i == len(timeline) or timeline[i]["person_id"] != timeline[start]["person_id"]:
            end_time = timeline[i]["time"] if i < len(timeline) else timeline[-1]["time"] + .5
            if timeline[start]["person_id"] and end_time - timeline[start]["time"] < minimum_hold:
                for item in timeline[start:i]:
                    item["person_id"] = None
                    item["evidence"] = "short/uncertain turn; retain group"
            start = i
    return timeline


def observe(source, clip, regions, audio_path=None, transcript=None, fps=2):
    people = [r for r in regions if r["kind"] == "person"]
    result = {"method": "audio-gated-mouth-motion-v1", "trained_speaker_model": False,
              "face_tracks": {}, "speaker_timeline": [], "note": "Conservative heuristic; uncertain turns retain all people."}
    if not people:
        return result
    try:
        import cv2
        import numpy as np
    except ImportError:
        result["note"] = "Local face observations unavailable; supplied regions are retained in full."
        return result
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        result["note"] = "Source cannot be sampled locally; retain supplied regions."
        return result
    audio = wave.open(str(audio_path), "rb") if audio_path else None
    observations, previous = [], {}
    duration = clip["end"] - clip["start"]
    # Sample only this clip; seeking avoids scanning hours of source material.
    try:
        for index in range(max(1, math.ceil(duration * fps))):
            t = min(index / fps, duration - .001)
            absolute = clip["start"] + t
            capture.set(cv2.CAP_PROP_POS_MSEC, absolute * 1000)
            ok, image = capture.read()
            if not ok:
                observations.append({"time": round(t, 3), "speech": False, "mouth_activity": {}})
                previous.clear()
                continue
            height, width = image.shape[:2]
            activities = {}
            for person in people:
                x, y, w, h = person["rect"]
                left, top, rw, rh = int(x * width), int(y * height), max(2, int(w * width)), max(2, int(h * height))
                region_image = image[top:top + rh, left:left + rw]
                if not region_image.size:
                    continue
                gray = cv2.cvtColor(region_image, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(24, 24))
                subject = person.get("subject")
                if len(faces):
                    # Inside a known camera feed, prefer the largest face.
                    fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                    subject = [(left + int(fx)) / width, (top + int(fy)) / height, int(fw) / width, int(fh) / height]
                if not subject:
                    continue
                result["face_tracks"].setdefault(person["id"], []).append({"time": round(t, 3), "rect": subject,
                    "detected": bool(len(faces))})
                fx, fy, fw, fh = subject
                patch = image[int(fy * height):max(int(fy * height) + 1, int((fy + fh) * height)),
                              int(fx * width):max(int(fx * width) + 1, int((fx + fw) * width))]
                if not patch.size:
                    continue
                patch = cv2.resize(cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY), (96, 96)).astype(np.float32)
                activity = 0.0
                old = previous.get(person["id"]) if len(faces) else None
                if old is not None:
                    shift, correlation = cv2.phaseCorrelate(old, patch)
                    if correlation > .1 and abs(shift[0]) < 7 and abs(shift[1]) < 7:
                        aligned = cv2.warpAffine(patch, np.float32([[1, 0, -shift[0]], [0, 1, -shift[1]]]), (96, 96), borderMode=cv2.BORDER_REFLECT)
                        difference = np.abs(aligned - old)
                        mouth = float(np.mean(difference[54:88, 18:78]))
                        upper = float(np.mean(difference[12:44, 18:78]))
                        activity = max(0, mouth - upper)
                previous[person["id"]] = patch if len(faces) else None
                activities[person["id"]] = round(activity, 3)
            speech = bool(transcript and any(s["start"] <= absolute <= s["end"] for s in transcript["segments"]))
            if audio:
                rate = audio.getframerate()
                audio.setpos(min(audio.getnframes(), max(0, int(absolute * rate))))
                values = np.frombuffer(audio.readframes(int(rate * .25)), dtype='<i2').astype(np.float32)
                speech = speech and bool(values.size and float(np.sqrt(np.mean(values ** 2))) > 60)
            observations.append({"time": round(t, 3), "speech": speech, "mouth_activity": activities})
    finally:
        capture.release()
        if audio:
            audio.close()
    result["observations"] = observations
    if len(people) > 2:
        result["speaker_timeline"] = stable_speakers(observations, [p["id"] for p in people])
    return result
