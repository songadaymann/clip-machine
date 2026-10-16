import array
import json
import math
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

from clip_machine.engine import (analyze_job, caption_preview, create_job, edit_captions, edit_clip, export_job,
                                 suggest_caption_emphasis)
from clip_machine.media import energy_features, probe, run
from clip_machine.providers import CLIProvider, CachedProvider, parse_json
from clip_machine.captions import caption_plan, write_caption_files
from clip_machine.selection import deduplicate, select_clips, validate_options
from clip_machine.server import ReviewServer
from clip_machine.storage import JobStore, write_json
from clip_machine.transcript import MLXTranscriber, chunks, normalize


class FixtureProvider:
    name = "test-fixture"

    def __init__(self):
        self.calls = 0
        self.fail_audit = False
        self.fake_ids = False

    def complete(self, prompt, schema):
        self.calls += 1
        if "summary" in schema["properties"]:
            return {"summary": "A complete fictional story.", "caveats": "Preserve the ending."}
        if "clips" in schema["properties"]:
            clips = json.loads(prompt.split("Clips (data): ", 1)[1])
            return {"clips": [{"id": c["id"], "words": [c["words"][-1]["id"]]} for c in clips]}
        if "reviews" in schema["properties"]:
            import re
            identities = list(dict.fromkeys(re.findall(r'"id": "([a-f0-9]{12})"', prompt)))
            return {"reviews": [] if self.fail_audit else [{"id": i, "faithful": True,
                    "standalone": True, "context_note": "Full story preserved."} for i in identities]}
        return {"candidates": [{"title": "A fictional complete story", "start_segment": "invented" if self.fake_ids else "s000000",
                "end_segment": "s000001", "hook": 86, "flow": 90, "value": 80, "reason": "Complete setup and payoff.",
                "context_note": "Faithful.", "topic": "testing", "standalone": True, "faithful": True}]}


def transcript():
    return {"language": "en", "segments": [
        {"start": 0, "end": 3, "text": "Here is the setup.", "speaker": "host"},
        {"start": 3, "end": 6, "text": "And here is the payoff.", "speaker": "host"},
        {"start": 6, "end": 12, "text": "Now a different thought.", "speaker": "guest"}]}


class LogicTests(unittest.TestCase):
    def test_empty_instantaneous_decoder_segments_do_not_abort_real_speech(self):
        raw = transcript()
        raw["segments"].insert(1, {"start": 3, "end": 3, "text": "", "words": []})
        raw["segments"].append({"start": 12, "end": 12, "text": "  ", "words": []})
        result = normalize(raw, 12)
        self.assertEqual(len(result["segments"]), 3)
        self.assertEqual([s["id"] for s in result["segments"]], ["s000000", "s000001", "s000002"])
        self.assertEqual(result["segments"][1]["text"], "And here is the payoff.")

    def test_content_with_reversed_or_zero_length_timestamps_still_fails(self):
        for start, end in ((3, 3), (3, 2), (11, 20)):
            raw = transcript()
            raw["segments"][0].update(start=start, end=end)
            with self.assertRaises(ValueError):
                normalize(raw, 12)

    def test_precise_word_times_are_preserved_across_coarse_segment_edges(self):
        raw = {"segments": [
            {"start": 3.58, "end": 4.94, "text": "A complete thought.", "words": [
                {"start": 3.23, "end": 3.8, "word": "A"},
                {"start": 3.8, "end": 4.5, "word": "complete"},
                {"start": 4.5, "end": 5.21, "word": "thought."}]}]}
        segment = normalize(raw, 12)["segments"][0]
        self.assertEqual((segment["start"], segment["end"]), (3.23, 5.21))
        self.assertEqual(segment["words"][0]["start"], 3.23)
        self.assertEqual(segment["words"][-1]["end"], 5.21)

    def test_word_boundary_drift_is_bounded_and_episode_end_rounding_is_clamped(self):
        raw = {"segments": [{"start": 1, "end": 3, "text": "A thought.", "words": [
            {"start": .49, "end": 2, "word": "A"}, {"start": 2, "end": 3, "word": "thought."}]}]}
        with self.assertRaises(ValueError):
            normalize(raw, 12)
        raw["segments"][0].update(start=10, end=12.2)
        raw["segments"][0]["words"] = [{"start": 10, "end": 12.2, "word": "Ending."}]
        result = normalize(raw, 12)["segments"][0]
        self.assertEqual(result["end"], 12)
        self.assertEqual(result["words"][0]["end"], 12)

    def test_asr_cache_survives_validation_failure_and_checks_audio_and_model(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        raw = transcript()
        raw["segments"][0]["end"] = 20
        transcribe = Mock(return_value=raw)
        with tempfile.TemporaryDirectory() as directory:
            audio, cache = Path(directory) / "audio.wav", Path(directory) / "asr-cache.json"
            audio.write_bytes(b"test audio")
            with patch.dict("sys.modules", {"mlx_whisper": SimpleNamespace(transcribe=transcribe)}):
                runner = MLXTranscriber("test-model", raw_cache=cache)
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        runner.transcribe(audio, 12)
                self.assertTrue(cache.exists())
                self.assertEqual(transcribe.call_count, 1)
                # A changed audio file or model must not reuse stale timings.
                audio.write_bytes(b"different audio")
                with self.assertRaises(ValueError):
                    runner.transcribe(audio, 12)
                with self.assertRaises(ValueError):
                    MLXTranscriber("different-model", raw_cache=cache).transcribe(audio, 12)
                self.assertEqual(transcribe.call_count, 3)

    def test_import_rejects_nonfinite_and_out_of_range_times(self):
        for bad in (float("nan"), float("inf"), -1, True, 15):
            raw = transcript()
            raw["segments"][0]["start"] = bad
            with self.assertRaises(ValueError):
                normalize(raw, 12)

    def test_missing_whisperx_word_alignment_falls_back_to_whole_segment(self):
        raw = transcript()
        raw["segments"][0]["words"] = [{"word": "Here", "start": 0, "end": 1}, {"word": "unknown"}]
        normalized = normalize(raw, 12)
        self.assertEqual(normalized["timing"], "segment")
        self.assertEqual(normalized["segments"][0]["text"], "Here is the setup.")

    def test_captions_are_relative_to_manual_trim_and_escape_ass_instructions(self):
        normalized = normalize(transcript(), 12)
        normalized["segments"][0]["text"] = r"{\pos(0,0)} <script>"
        plan = caption_plan(normalized, {"start": 1.25, "end": 4.5, "title": "Trimmed"})
        self.assertEqual(plan["chunks"][0]["start"], 0)
        self.assertEqual(plan["chunks"][-1]["end"], 3.25)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_caption_files(folder, plan)
            self.assertNotIn(r"{\pos", (folder / "captions.ass").read_text())
            self.assertIn("&lt;script&gt;", (folder / "captions.vtt").read_text())

    def test_invalid_model_boundaries_and_missing_context_reviews_are_excluded(self):
        for scenario in ("fake_ids", "fail_audit"):
            provider = FixtureProvider()
            setattr(provider, scenario, True)
            result = select_clips(normalize(transcript(), 12), {"segments": {}}, 12,
                                  {"min_seconds": 3, "max_seconds": 12}, provider, lambda *_: None)
            self.assertEqual(result["clips"], [])
            self.assertTrue(result["rejected_candidates"])

    def test_contained_duplicate_is_removed_even_when_iou_is_small(self):
        clips = [{"id": "a", "start": 0, "end": 90, "score": 85},
                 {"id": "b", "start": 30, "end": 50, "score": 80},
                 {"id": "c", "start": 100, "end": 130, "score": 70}]
        self.assertEqual([c["id"] for c in deduplicate(clips)], ["a", "c"])

    def test_chunk_overlap_covers_clip_length(self):
        segments = [{"start": i * 10, "end": i * 10 + 10, "text": "word " * 100} for i in range(30)]
        batches = list(chunks(segments, max_chars=7000, overlap_seconds=60))
        self.assertGreater(len(batches), 1)
        self.assertLessEqual(batches[1][0]["start"], batches[0][-1]["end"] - 60)
        self.assertEqual(batches[-1][-1], segments[-1])

    def test_cached_analysis_avoids_new_calls_and_model_json_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FixtureProvider()
            cached = CachedProvider(provider, directory)
            schema = {"properties": {"summary": {}}}
            self.assertEqual(cached.complete("same", schema), cached.complete("same", schema))
            self.assertEqual(provider.calls, 1)
        self.assertEqual(parse_json('```json\n{"ok":true}\n```'), {"ok": True})
        with self.assertRaises(ValueError):
            parse_json('Some commentary {"ok":true}')

    def test_options_reject_reversed_ranges_and_coerced_booleans(self):
        for options in ({"min_seconds": 60, "max_seconds": 20}, {"count": True},
                        {"count": 0}, {"auto_render": "false"}, {"max_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                validate_options(options)

    def test_claude_account_cap_is_readable_without_api_key_fallback(self):
        import subprocess
        response = subprocess.CompletedProcess([], 1, json.dumps({"is_error": True, "result": "Spending cap reached"}).encode(), b"")
        with patch("clip_machine.providers.subprocess.run", return_value=response) as execute:
            with self.assertRaisesRegex(RuntimeError, "claude analysis failed: Spending cap reached$"):
                CLIProvider("claude").complete("a harmless example", {"type": "object"})
            self.assertNotIn("ANTHROPIC_API_KEY", execute.call_args.kwargs["env"])
            self.assertIn("--tools", execute.call_args.args[0])

    def test_energy_is_relative_to_speaker_not_recording_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            results = []
            segments = normalize(transcript(), 12)["segments"]
            for gain in (2000, 6000):
                file = Path(directory) / (str(gain) + ".wav")
                with wave.open(str(file), "wb") as audio:
                    audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    for second in range(12):
                        amplitude = gain * (2 if second >= 3 and second < 6 else 1)
                        data = array.array("h", [int(amplitude * math.sin(i * 2 * math.pi * 220 / 16000)) for i in range(16000)])
                        audio.writeframes(data.tobytes())
                results.append(energy_features(file, segments))
            for identity in results[0]["segments"]:
                self.assertAlmostEqual(results[0]["segments"][identity]["relative_db"], results[1]["segments"][identity]["relative_db"], delta=.03)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="clip tests ' safe ")
        cls.folder = Path(cls.temporary.name)
        cls.source = cls.folder / "episode.mp4"
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
             "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=16000", "-t", "12", "-c:v", "libx264",
             "-preset", "ultrafast", "-c:a", "aac", str(cls.source)])
        cls.transcript_path = cls.folder / "transcript.json"
        write_json(cls.transcript_path, transcript())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=self.folder)
        self.store = JobStore(Path(self.work.name) / "jobs")

    def tearDown(self):
        self.work.cleanup()

    def analyze(self, layout="original", source=None):
        job = create_job(self.store, source or self.source, {"min_seconds": 3, "max_seconds": 12, "layout": layout}, self.transcript_path)
        return analyze_job(self.store, job["id"], FixtureProvider())

    def test_full_pipeline_export_manual_trim_and_revision(self):
        job = self.analyze()
        self.assertEqual(len(job["clips"]), 1)
        c = job["clips"][0]
        job = edit_clip(self.store, job["id"], c["id"], 1.25, 5.75, "approved")
        self.assertEqual(job["clips"][0]["revision"], 2)
        self.assertEqual(job["clips"][0]["original_start"], 0)
        job = export_job(self.store, job["id"])
        export = job["clips"][0]["export"]
        self.assertAlmostEqual(export["duration"], 4.5, delta=.1)
        self.assertTrue(export["burn_captions"])
        self.assertEqual(job["status"], "complete")
        captions = (self.store.folder(job["id"]) / export["srt"]).read_text()
        self.assertIn("00:00:00,000", captions)
        self.assertIn("00:00:04,500", captions)
        edited = edit_clip(self.store, job["id"], c["id"], 1.5, 5.5)
        self.assertIsNone(edited["clips"][0]["export"])
        self.assertEqual(len(edited["feedback"]), 2)

    def test_engine_saves_raw_asr_and_filters_empty_decoder_entries(self):
        from types import SimpleNamespace
        raw = transcript()
        raw["segments"].insert(0, {"start": 0, "end": 0, "text": "", "words": []})
        raw["segments"].append({"start": 12, "end": 12, "text": "", "words": []})
        job = create_job(self.store, self.source, {"min_seconds": 3, "max_seconds": 12})
        with patch.dict("sys.modules", {"mlx_whisper": SimpleNamespace(transcribe=lambda *a, **kw: raw)}):
            result = analyze_job(self.store, job["id"], FixtureProvider())
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["clips"]), 1)
        cache = json.loads((self.store.folder(job["id"]) / "asr-cache.json").read_text())
        self.assertEqual(len(cache["result"]["segments"]), 5)
        canonical = json.loads((self.store.folder(job["id"]) / "transcript.json").read_text())
        self.assertEqual(len(canonical["segments"]), 3)

    def test_portrait_is_720_by_1280(self):
        job = self.analyze("portrait")
        job = export_job(self.store, job["id"])
        exported = job["clips"][0]["export"]
        self.assertEqual((exported["width"], exported["height"]), (720, 1280))

    def test_audio_only_renders_audio_and_captioned_waveform(self):
        source = Path(self.work.name) / "source.wav"
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(self.source), "-vn", str(source)])
        for layout in ("original", "portrait"):
            job = export_job(self.store, self.analyze(layout, source)["id"])
            export = job["clips"][0]["export"]
            self.assertEqual(export["media"].endswith(".mp4"), layout == "portrait")
            self.assertEqual(export["burn_captions"], layout == "portrait")

    def test_changed_source_and_invalid_trims_are_refused(self):
        local_source = Path(self.work.name) / "local.mp4"
        shutil.copyfile(self.source, local_source)
        job = self.analyze(source=local_source)
        for start, end in ((-1, 4), (6, 4), (0, 100), (float("nan"), 4)):
            with self.assertRaises(ValueError):
                edit_clip(self.store, job["id"], job["clips"][0]["id"], start, end)
        local_source.touch()
        with self.assertRaisesRegex(ValueError, "source file changed"):
            export_job(self.store, job["id"])

    def test_analysis_stores_emphasis_and_caption_edits_layer_without_touching_transcript(self):
        job = self.analyze("portrait")
        c = job["clips"][0]
        self.assertEqual(c["revision"], 1)
        suggestion = c["captions"]["emphasis_suggestion"]
        self.assertEqual((suggestion["source"], suggestion["error"], len(suggestion["refs"])), ("test-fixture", None, 1))
        transcript_file = self.store.folder(job["id"]) / "transcript.json"
        before = transcript_file.read_bytes()
        preview = caption_preview(self.store, job["id"], c["id"])
        first = preview["words"][0]["ref"]
        job = export_job(self.store, job["id"])
        self.assertEqual(job["clips"][0]["export"]["caption_style"], "pop-v1")
        job = edit_captions(self.store, job["id"], c["id"], {"hidden": {first: True}, "hook": {"enabled": True, "text": " Setup "}})
        edited = job["clips"][0]
        self.assertEqual((edited["revision"], edited["export"]), (2, None))
        self.assertEqual(edited["captions"]["hook"], {"enabled": True, "text": "Setup"})
        self.assertEqual(job["feedback"][-1]["captions"]["hidden"], {first: True})
        self.assertEqual(transcript_file.read_bytes(), before)
        unchanged = edit_captions(self.store, job["id"], c["id"], {"hidden": {first: True}})
        self.assertEqual(unchanged["clips"][0]["revision"], 2)
        # Overrides reference source words, so they survive a trim.
        trimmed = edit_clip(self.store, job["id"], c["id"], 0.5, 5.5)
        words = caption_preview(self.store, job["id"], c["id"])["words"]
        self.assertTrue(next(w for w in words if w["ref"] == first)["hidden"])
        self.assertEqual(trimmed["clips"][0]["revision"], 3)
        with self.assertRaisesRegex(ValueError, "Unknown caption word"):
            edit_captions(self.store, job["id"], c["id"], {"emphasis": {"s999999:0": True}})
        exported = export_job(self.store, job["id"])["clips"][0]["export"]
        srt = (self.store.folder(job["id"]) / exported["srt"]).read_text()
        self.assertNotIn("Here ", srt)
        self.assertIn("SETUP", (self.store.folder(job["id"]) / exported["ass"]).read_text())

    def test_failed_emphasis_request_keeps_previous_suggestion(self):
        job = self.analyze()
        c = job["clips"][0]

        class Failing(FixtureProvider):
            name = "failing-fixture"  # A different provider misses the cached success.

            def complete(self, prompt, schema):
                raise RuntimeError("fixture quota reached")

        job = suggest_caption_emphasis(self.store, job["id"], c["id"], Failing())
        captions = job["clips"][0]["captions"]
        self.assertEqual(captions["emphasis_suggestion"], c["captions"]["emphasis_suggestion"])
        self.assertEqual(captions["emphasis_error"], "fixture quota reached")
        self.assertEqual(job["clips"][0]["revision"], 1)
        self.assertIn("Caption emphasis unavailable", job["message"])

    def test_rejected_clips_are_not_exported(self):
        job = self.analyze()
        edit_clip(self.store, job["id"], job["clips"][0]["id"], decision="rejected")
        with self.assertRaisesRegex(ValueError, "No selected clips"):
            export_job(self.store, job["id"])

    def test_loopback_api_range_requests_tokens_and_path_isolation(self):
        job = self.analyze()
        server = ReviewServer(self.store, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            request = urllib.request.Request(base + "/api/jobs/" + job["id"] + "/source", headers={"Range": "bytes=0-31"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(len(response.read()), 32)
            request = urllib.request.Request(base + "/api/jobs", data=b"{}", headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + "/api/jobs", data=b"{}", headers={"Content-Type": "application/json", "X-Clip-Token": server.token, "Origin": "https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + "/api/jobs/" + job["id"] + "/edit", data=json.dumps({"clip_id": job["clips"][0]["id"], "decision": "approved"}).encode(), headers={"Content-Type": "application/json", "X-Clip-Token": server.token})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["clips"][0]["decision"], "approved")
            clip_id = job["clips"][0]["id"]
            with urllib.request.urlopen(base + "/api/jobs/" + job["id"] + "/captions/" + clip_id) as response:
                preview = json.load(response)
            self.assertEqual(preview["mode"], "clean")
            change = json.dumps({"clip_id": clip_id, "changes": {"mode": "verbatim"}}).encode()
            request = urllib.request.Request(base + "/api/jobs/" + job["id"] + "/captions", data=change, headers={"Content-Type": "application/json", "X-Clip-Token": server.token})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["clips"][0]["captions"]["mode"], "verbatim")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base + "/api/jobs/" + job["id"] + "/captions", data=change, headers={"Content-Type": "application/json"}))
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/api/jobs/" + job["id"] + "/files/job.json")
            with self.assertRaises(ValueError):
                self.store.folder("../../LICENSE")
        finally:
            server.shutdown()
            server.server_close()
            server.executor.shutdown()


if __name__ == "__main__":
    unittest.main()
