import json
import tempfile
import unittest
from pathlib import Path

from clip_machine.captions import (ACCENT, FONT_FILE, FONT_NAME, SAFE_BOTTOM, SAFE_TOP, caption_anchor, caption_plan,
                                   content_boxes, display, line_width, suggest_emphasis, validate_caption_edit,
                                   write_caption_files)
from clip_machine.transcript import normalize

ROOT = Path(__file__).resolve().parents[1]


def spoken(*sentences, gap=.05, start=0):
    """A word-timed Whisper-style transcript, one segment per sentence, 0.3 s per word."""
    segments, at = [], start
    for sentence in sentences:
        words = []
        for token in sentence.split():
            words.append({"word": token, "start": round(at, 3), "end": round(at + .3, 3)})
            at += .3 + gap
        segments.append({"start": words[0]["start"], "end": words[-1]["end"], "text": sentence, "words": words})
    return normalize({"segments": segments}, at + 1)


def plan_for(transcript, **captions):
    end = transcript["segments"][-1]["end"]
    return caption_plan(transcript, {"start": 0, "end": end, "title": "A test clip", "captions": captions})


def visible_text(plan):
    return " ".join(w["text"] for w in plan["words"] if not w["hidden"])


class CleanupTests(unittest.TestCase):
    def test_filler_restarts_and_set_off_asides_are_hidden(self):
        plan = plan_for(spoken("Um... As we see... As we see fit.", "The judges can... You know... judge it.",
                               "It was, like, minutes ago.", "I mean, that's probably what I mean."))
        self.assertEqual(visible_text(plan), "As we see fit. The judges can judge it. It was, minutes ago. that's probably what I mean.")
        reasons = {w["text"]: w["rule"] for w in plan["words"] if w["hidden"]}
        self.assertEqual(reasons["Um..."], "filler")
        self.assertEqual(reasons["see..."], "restart")
        self.assertEqual(reasons["know..."], "aside")

    def test_meaningful_words_are_kept(self):
        plan = plan_for(spoken("I like this game.", "You know what I mean?", "So, some people stay.",
                               "Very, very good.", "It's like, how can we?"))
        self.assertFalse(any(w["hidden"] for w in plan["words"]))

    def test_stutters_and_contraction_restarts(self):
        plan = plan_for(spoken("There... There is... There's wiggle room.", "I, I think so."))
        self.assertEqual(visible_text(plan), "There's wiggle room. I think so.")

    def test_audio_words_never_change_only_visibility(self):
        transcript = spoken("Um... the numbers mean something.")
        before = json.dumps(transcript)
        plan = plan_for(transcript)
        self.assertEqual(json.dumps(transcript), before)
        self.assertEqual(len(plan["words"]), 5)
        self.assertEqual(plan["words"][0]["start"], 0)


class PhraseTests(unittest.TestCase):
    def test_balanced_phrases_fit_the_frame(self):
        plan = plan_for(spoken("The numbers mean something.", "We got a lot of our themes kind of flowing together."))
        lookup = {w["ref"]: w for w in plan["words"]}
        lines = [" ".join(display(lookup[r]["text"]) for r in c["refs"]) for c in plan["chunks"]]
        self.assertEqual(lines[:2], ["THE NUMBERS", "MEAN SOMETHING"])
        for line in lines:
            self.assertFalse(line.endswith((" A", " LOT", " KIND")), line)
        self.assertTrue(all(line_width([lookup[r] for r in c["refs"]], 76) <= 580 for c in plan["chunks"]))
        self.assertTrue(all(1 <= len(c["refs"]) <= 4 for c in plan["chunks"]))

    def test_chunks_follow_speech_without_overlap(self):
        plan = plan_for(spoken("First thought here.", "Second one.", gap=.1))
        for previous, following in zip(plan["chunks"], plan["chunks"][1:]):
            self.assertLessEqual(previous["end"], following["start"])

    def test_manual_overrides_layer_on_suggestions_and_verbatim_shows_everything(self):
        transcript = spoken("Um... the numbers mean something.")
        refs = [w["ref"] for w in plan_for(transcript)["words"]]
        plan = plan_for(transcript, edits={"hidden": {refs[0]: False, refs[1]: True}, "emphasis": {refs[2]: True}},
                        emphasis_suggestion={"refs": [refs[3]]})
        states = [(w["hidden"], w["emphasis"]) for w in plan["words"]]
        self.assertEqual(states, [(False, False), (True, False), (False, True), (False, True), (False, False)])
        verbatim = plan_for(transcript, mode="verbatim", edits={"hidden": {refs[1]: True}})
        self.assertEqual(visible_text(verbatim), "Um... the numbers mean something.")

    def test_segment_only_transcript_keeps_whole_segments(self):
        transcript = normalize({"segments": [{"start": 0, "end": 3, "text": "Um, here is the setup."},
                                             {"start": 3, "end": 6, "text": "And the payoff."}]}, 6)
        plan = plan_for(transcript)
        self.assertEqual(plan["timing"], "segment")
        self.assertEqual([(c["start"], c["end"]) for c in plan["chunks"]], [(0, 3), (3, 6)])
        self.assertEqual(visible_text(plan), "here is the setup. And the payoff.")


class FileTests(unittest.TestCase):
    def test_ass_uses_bundled_font_red_highlight_and_blocks_markup(self):
        transcript = spoken(r"Watch {\pos(0,0)} the numbers.")
        refs = [w["ref"] for w in plan_for(transcript)["words"]]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_caption_files(folder, plan_for(transcript, emphasis_suggestion={"refs": [refs[3]]}), True, [(0, 99, 700)])
            ass = (folder / "captions.ass").read_text()
            self.assertIn("Style: Caption," + FONT_NAME, ass)
            self.assertIn(ACCENT + "\\fscx100", ass)
            self.assertIn(ACCENT + "\\fscx120\\fscy120}NUMBERS", ass)
            self.assertIn("\\pos(360,700)", ass)
            self.assertIn("｛＼POS(0,0)｝", ass)
            self.assertNotIn("{\\POS", ass)
            self.assertNotIn("Hook,,", ass)
            self.assertIn("the numbers.", (folder / "captions.srt").read_text())
            write_caption_files(folder, plan_for(transcript, hook={"enabled": True, "text": "Numbers <matter>"}), True)
            self.assertIn("Hook,,0,0,0,,{\\an8\\pos(360,%d)}NUMBERS <MATTER>" % SAFE_TOP, (folder / "captions.ass").read_text())

    def test_bundled_font_has_its_license_and_provenance(self):
        fonts = ROOT / "clip_machine" / "fonts"
        self.assertGreater((fonts / FONT_FILE).stat().st_size, 100000)
        self.assertIn("SIL Open Font License", (fonts / "OFL.txt").read_text())
        self.assertIn("Omnibus-Type/Archivo", (ROOT / "ASSETS.md").read_text())


class PlacementTests(unittest.TestCase):
    media = {"width": 1920, "height": 1080}

    def scene(self, subject=None):
        return {"regions": [{"id": "game", "kind": "gameplay", "rect": [0, 0, 1, .9], "subject": None},
                            {"id": "a", "kind": "person", "rect": [0, 0, .5, .5], "subject": subject},
                            {"id": "b", "kind": "person", "rect": [.5, 0, .5, .5], "subject": None}],
                "panels": [{"region_id": "game", "source": [0, 0, 1, .9], "destination": [0, 0, 720, 760]},
                           {"region_id": "a", "source": [0, 0, .5, .5], "destination": [0, 760, 360, 344]},
                           {"region_id": "b", "source": [.5, 0, .5, .5], "destination": [360, 760, 360, 344]}]}

    def test_captions_sit_between_game_and_faces_inside_platform_safe_zone(self):
        boxes = content_boxes(self.scene(), self.media)
        game_bottom, faces_top = boxes[0]["box"][1] + boxes[0]["box"][3], boxes[1]["box"][1]
        y = caption_anchor(boxes)
        self.assertTrue(game_bottom <= y - 60 and y + 60 <= faces_top, (game_bottom, y, faces_top))
        self.assertTrue(SAFE_TOP + 60 <= y <= SAFE_BOTTOM - 60)

    def test_tracked_faces_are_avoided(self):
        everyone = [{"kind": "person", "box": (0, 0, 720, 1104), "subject": (200, 640, 320, 300)}]
        y = caption_anchor(everyone)
        self.assertFalse(640 - 60 < y < 940 + 60, y)


class EmphasisTests(unittest.TestCase):
    def test_suggestions_are_grounded_spaced_and_failures_recorded(self):
        transcript = spoken("Numbers matter more than vibes when judges score games fairly.", gap=.05)
        clip = {"id": "abc123abc123", "title": "Numbers", "start": 0, "end": transcript["segments"][-1]["end"]}

        class Fixture:
            name, model = "test-fixture", None

            def complete(self, prompt, schema):
                return {"clips": [{"id": "abc123abc123", "words": ["w0", "w0", "w1", "invented", "w9"]}]}

        result = suggest_emphasis(transcript, [clip], Fixture())[clip["id"]]
        words = {w["ref"]: w["text"] for w in plan_for(transcript)["words"]}
        self.assertEqual([words[r] for r in result["refs"]], ["Numbers", "fairly."])
        self.assertIsNone(result["error"])

        class Failing(Fixture):
            def complete(self, prompt, schema):
                raise RuntimeError("fixture quota reached")

        failed = suggest_emphasis(transcript, [clip], Failing())[clip["id"]]
        self.assertEqual((failed["refs"], failed["error"]), ([], "fixture quota reached"))

    def test_manual_edits_are_validated(self):
        refs = {"s000000:0"}
        validate_caption_edit({"hidden": {"s000000:0": None}, "hook": {"enabled": True, "text": "Hi"}}, refs)
        for bad in ({"hidden": {"s000000:9": True}}, {"emphasis": {"s000000:0": "yes"}}, {"mode": "loud"},
                    {"hook": {"text": "x" * 121}}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                validate_caption_edit(bad, refs)


if __name__ == "__main__":
    unittest.main()
