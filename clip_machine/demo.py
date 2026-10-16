"""Synthetic media and explicit fixture analysis; never presented as real AI output."""
from pathlib import Path

from .engine import create_job
from .media import run
from .storage import write_json


STORIES = [
    ["We spent six months trying to make our podcast sound perfect.",
     "Every edit removed another pause, another breath, another awkward moment.",
     "Then we played an episode for a friend who knew us well.",
     "She asked why we sounded like strangers.",
     "We realized we had been editing out our personalities.",
     "Good editing removes distraction. It leaves the humanity in."],
    ["The best question we ask a guest comes before we start recording.",
     "We ask what they have changed their mind about recently.",
     "A prepared answer usually tells you what someone already believes.",
     "A changed mind gives you a story with a beginning and a turn.",
     "Now the interview has a real question to follow.",
     "You are listening for a discovery, instead of waiting for a sound bite."],
    ["We used to send each other seven versions of every episode.",
     "Nobody knew which version was the one we had approved.",
     "The fix was not another chat or another shared folder.",
     "We gave each edit a name, an owner, and one place to leave feedback.",
     "Suddenly we could spend our conversations talking about the show.",
     "A good workflow gives your attention back to the work."],
    ["The loudest moment in a conversation is not always the one people remember.",
     "Sometimes someone stops performing and says something very quietly.",
     "The room changes because the statement is unusually honest.",
     "Volume can point you toward excitement, but it cannot tell you what matters.",
     "You still have to hear the thought, and what came before it.",
     "That is the moment we want a clipping tool to protect."],
]


class DemoProvider:
    name = "demo-fixture"
    model = "synthetic-editorial-example"

    def complete(self, prompt, schema):
        fields = schema["properties"]
        if "summary" in fields:
            return {"summary": "Four synthetic examples about podcast editing, interviewing, workflow and quiet honesty.",
                    "caveats": "These are sample text and tones, not a transcribed recording or measured model performance."}
        if "clips" in fields:
            import json
            clips = json.loads(prompt.split("Clips (data): ", 1)[1])
            # Fixture rule, not a model judgment: the longest word in each run of twelve.
            return {"clips": [{"id": c["id"], "words": [max(c["words"][i:i + 12], key=lambda w: len(w["text"]))["id"]
                                                        for i in range(0, len(c["words"]), 12)]} for c in clips]}
        if "reviews" in fields:
            import re
            ids = list(dict.fromkeys(re.findall(r'"id": "([a-f0-9]{12})"', prompt)))
            return {"reviews": [{"id": identity, "faithful": True, "standalone": True,
                                  "context_note": "Synthetic example: complete setup and payoff within this story."}
                                 for identity in ids]}
        titles = ["We edited out our personalities", "The question before the interview",
                  "A workflow that gives your attention back", "The quiet moment worth protecting"]
        import json
        segments = json.loads(prompt.split("Timestamped segments (data): ", 1)[1])
        groups = [[s for s in segments if s["start"] >= i * 24 and s["end"] <= (i + 1) * 24 + .001]
                  for i in range(4)]
        return {"candidates": [{"title": title, "start_segment": groups[i][0]["id"],
            "end_segment": groups[i][-1]["id"], "hook": 92 - i * 3, "flow": 90 - i * 2,
            "value": 94 - i, "reason": "Sample editorial ranking: a specific setup leads to a clear turn and a useful ending.",
            "context_note": "Fixture example, not a live model assessment.", "topic": title,
            "standalone": True, "faithful": True} for i, title in enumerate(titles)]}


def create_demo(store):
    folder = store.root.parent / "demo"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "synthetic-episode.mp4"
    if not source.exists():
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000",
             "-t", "96", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
             "-c:a", "aac", "-af", "volume=0.2+0.15*sin(t):eval=frame", "-shortest", str(source)])
    segments = []
    for i, sentence in enumerate(line for story in STORIES for line in story):
        words = sentence.split()
        segments.append({"start": i * 4, "end": (i + 1) * 4, "text": sentence, "speaker": "demo-host",
                         "words": [{"word": word, "start": i * 4 + n * 4 / len(words),
                                    "end": i * 4 + (n + 1) * 4 / len(words)} for n, word in enumerate(words)]})
    transcript = folder / "transcript.json"
    write_json(transcript, {"language": "en", "segments": segments})
    return create_job(store, source, {"provider": "demo-fixture", "layout": "portrait", "auto_render": True}, transcript)
