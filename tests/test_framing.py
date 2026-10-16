"""Policy/visibility contracts and real FFmpeg composition, with no model calls."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_machine.framing import choose_layout, fallback_plan, manual_plan, panels_for, validate_plan
from clip_machine.tracking import face_window, stable_speakers
from clip_machine.vision import VisualFramer, make_scene
from clip_machine.render import framing_preview, render_clip
from clip_machine.media import probe, run
from clip_machine.providers import CachedProvider, CLIProvider
from clip_machine.engine import create_job, edit_clip, save_framing, reframe_job
from clip_machine.storage import JobStore, write_json


def people(count=2):
    return [dict(id='p%d' % i, kind='person', label='Person %d' % i,
                 rect=[(i % 2) * .5, (i // 2) * .5, .5, .5], subject=None) for i in range(count)]


def cut():
    return dict(id='abc123', title='Synthetic example', start=0, end=2, revision=1,
                decision='unreviewed', export=None)


class FramingTests(unittest.TestCase):
    def test_two_faces_stay_visible_even_in_speaker_mode(self):
        for policy in ('auto', 'speaker', 'two_people', 'group'):
            plan = manual_plan(cut(), people(), policy)
            self.assertEqual(plan['scenes'][0]['layout'], 'two_people')
            self.assertEqual({p['region_id'] for p in plan['scenes'][0]['panels']}, {'p0', 'p1'})

    def test_game_and_faces_preserved_with_avatar_anywhere_in_game(self):
        for x, y in ((0, 0), (.7, 0), (0, .7), (.7, .7)):
            game = dict(id='game', label='Game', kind='gameplay', rect=[0, 0, .8, .8], subject=[x, y, .1, .1])
            scene = manual_plan(cut(), [game] + people(), 'speaker')['scenes'][0]
            self.assertEqual(scene['layout'], 'gameplay_people')
            self.assertEqual(scene['panels'][0]['source'], game['rect'])
            self.assertEqual(len(scene['panels']), 3)

    def test_group_focus_needs_sustained_unambiguous_speech(self):
        ids = [p['id'] for p in people(4)]
        def observations(scores, speech=True, count=20):
            return [dict(time=i*.5, speech=speech, mouth_activity=scores) for i in range(count)]
        for scores, speech in (({'p0': 7, 'p1': 6}, True), ({'p0': 7}, False), ({'p0': .9}, True)):
            self.assertTrue(all(t['person_id'] is None for t in stable_speakers(observations(scores, speech), ids)))
        timeline = stable_speakers(observations({'p0': 7, 'p1': 1}), ids)
        self.assertTrue(all(t['person_id'] is None for t in timeline[:6]))
        self.assertEqual(timeline[-1]['person_id'], 'p0')
        self.assertEqual(choose_layout(people(4), 'auto'), 'group')
        self.assertEqual(choose_layout(people(4), 'auto', 'p0'), 'speaker')
        self.assertEqual(choose_layout(people(4), 'group', 'p0'), 'group')
        short = stable_speakers(observations({'p0': 7}, count=9), ids)
        self.assertTrue(all(t['person_id'] is None for t in short))

    def test_invalid_or_occluded_compositions_fail(self):
        plan = manual_plan(cut(), people())
        bads = []
        bad = copy.deepcopy(plan); bad['scenes'][0]['panels'].pop(); bads.append(bad)
        bad = copy.deepcopy(plan); bad['clip_end'] = 3; bads.append(bad)
        bad = copy.deepcopy(plan); bad['scenes'][0]['start'] = .1; bads.append(bad)
        bad = copy.deepcopy(plan); bad['scenes'][0]['panels'][1]['destination'] = [0, 0, 720, 552]; bads.append(bad)
        bad = fallback_plan(cut(), 'test'); bad['scenes'][0]['panels'][0]['source'] = [.2, .2, .4, .4]; bads.append(bad)
        bad = copy.deepcopy(plan); bad['scenes'][0]['panels'][1]['destination'] = [0, 1100, 720, 180]; bads.append(bad)
        for bad in bads:
            with self.assertRaises(ValueError): validate_plan(bad, cut())
        for rect in ([0, 0, 2, 1], [0, 0, float('nan'), 1], [-.1, 0, .5, .5]):
            regions = people(); regions[0]['rect'] = rect
            with self.assertRaises(ValueError): manual_plan(cut(), regions)

    def test_face_window_keeps_motion_union(self):
        region = dict(id='p', kind='person', rect=[0, 0, 1, 1])
        subjects = [[.1, .2, .1, .2], [.7, .5, .1, .2]]
        window = face_window(region, subjects, [0, 0, 360, 344], dict(width=1920, height=1080))
        from clip_machine.framing import contains
        self.assertTrue(all(contains(window, s) for s in subjects))

    def test_image_cache_depends_on_pixels(self):
        class Fixture:
            name = 'fixture'
            calls = 0
            def complete(self, prompt, schema, images=None):
                self.calls += 1
                return {'calls': self.calls}
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(); image = Path(d)/'image.jpg'; image.write_bytes(b'first')
            cache = CachedProvider(f, Path(d)/'cache')
            cache.complete('prompt', {}, images=[image]); cache.complete('prompt', {}, images=[image])
            self.assertEqual(f.calls, 1)
            image.write_bytes(b'second'); cache.complete('prompt', {}, images=[image])
            self.assertEqual(f.calls, 2)

    def test_claude_images_use_structured_stdin_without_shell_or_api_fallback(self):
        import subprocess
        import base64
        with tempfile.TemporaryDirectory() as d:
            image = Path(d)/'frame.jpg'; image.write_bytes(b'fixture image')
            response = subprocess.CompletedProcess([], 0, b'{"result":"{\\"frames\\":[]}"}', b'')
            response.stdout = json.dumps({'result': json.dumps({'frames': []})}).encode()
            with patch('clip_machine.providers.subprocess.run', return_value=response) as execute:
                self.assertEqual(CLIProvider('claude').complete('inspect', {}, images=[image]), {'frames': []})
                args, kwargs = execute.call_args.args[0], execute.call_args.kwargs
                self.assertIn('--input-format', args)
                self.assertNotIn('ANTHROPIC_API_KEY', kwargs['env'])
                self.assertNotIn('shell', kwargs)
                event = json.loads(kwargs['input'])
                part = event['message']['content'][1]
                self.assertEqual(base64.b64decode(part['source']['data']), b'fixture image')

    def test_uncertain_scene_change_preserves_full_source(self):
        class Fixture:
            def complete(self, prompt, schema, images=None):
                return {'frames': [dict(index=i, regions=[dict(id='p0', kind='person', label='Person', rect=([0,0,500,500] if i<2 else [500,0,500,500]), subject=[], confidence=99)], note='') for i in range(5)]}
        samples = [dict(index=i, time=i*.5, source_time=i*.5, path='source-%02d.jpg'%i) for i in range(5)]
        with tempfile.TemporaryDirectory() as d, patch('clip_machine.vision.sample_frames', return_value=samples):
            plan = VisualFramer(Fixture()).analyze('unused', dict(width=1920,height=1080), cut(), d)
            self.assertTrue(plan['needs_review'])
            self.assertEqual(plan['scenes'][1]['layout'], 'full_frame')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class CompositionTests(unittest.TestCase):
    def test_real_multiscene_video_preserves_colors_duration_and_single_audio(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d); source = folder/'source.mp4'
            run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=black:s=640x360:r=30:d=2',
                 '-f','lavfi','-i','sine=frequency=440:duration=2', '-vf',
                 'drawbox=x=0:y=0:w=320:h=180:color=red:t=fill,drawbox=x=320:y=0:w=320:h=180:color=blue:t=fill,drawbox=x=0:y=180:w=640:h=180:color=green:t=fill',
                 '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)])
            clip = cut(); regions = [dict(id='game',kind='gameplay',label='Game',rect=[0,.5,1,.5],subject=None)] + people()
            clip['framing'] = manual_plan(clip, regions)
            first = clip['framing']['scenes'][0]; first['end'] = 1
            second = copy.deepcopy(first); second.update(start=1, end=2)
            second['panels'][1]['destination'], second['panels'][2]['destination'] = second['panels'][2]['destination'], second['panels'][1]['destination']
            clip['framing']['scenes'].append(second)
            result = render_clip(source, probe(source), {'segments':[]}, clip, folder/'exports', 'portrait', False)
            target = folder/result['media']
            self.assertEqual((result['width'], result['height']), (720,1280))
            self.assertAlmostEqual(result['duration'], 2, delta=.1)
            info = json.loads(run(['ffprobe','-v','error','-show_streams','-of','json',str(target)]))
            self.assertEqual(sum(s['codec_type']=='audio' for s in info['streams']), 1)
            from PIL import Image
            for t in (.5, 1.5):
                frame=folder/('frame%s.png'%t)
                run(['ffmpeg','-v','error','-y','-ss',str(t),'-i',str(target),'-frames:v','1',str(frame)])
                im=Image.open(frame)
                game=im.getpixel((360,380)); left=im.getpixel((180,920)); right=im.getpixel((540,920))
                self.assertGreater(game[1], game[0]+40)
                red, blue = (left,right) if t<1 else (right,left)
                self.assertGreater(red[0], red[2]+100); self.assertGreater(blue[2],blue[0]+100)
            # Engine edits invalidate framing as well as exports; previous plans remain recoverable.
            store=JobStore(folder/'jobs'); job=create_job(store,source)
            job.update(status='ready',media=probe(source),clips=[clip]);store.save(job)
            write_json(store.folder(job['id'])/'transcript.json',{'segments':[]})
            saved=save_framing(store,job['id'],clip['id'],regions)
            self.assertIsNone(saved['clips'][0]['export'])
            edited=edit_clip(store,job['id'],clip['id'],end=1.5)
            self.assertNotIn('framing',edited['clips'][0])
            self.assertTrue(edited['clips'][0]['framing_history'])


            # Exercise the framing HTTP adapter and its private-image whitelist.
            import threading
            import urllib.request
            import urllib.error
            from clip_machine.server import ReviewServer
            job=store.load(job['id']); c=job['clips'][0]
            sample_dir=store.folder(job['id'])/'framing'/'abc123-r8';sample_dir.mkdir(parents=True)
            sample=sample_dir/'source-00.jpg'
            run(['ffmpeg','-v','error','-y','-i',str(source),'-frames:v','1',str(sample)])
            c['framing']=manual_plan(c,regions)
            c['framing']['samples']=[dict(index=0,time=0,file=str(sample.relative_to(store.folder(job['id']))))]
            c['export']={'media':'exports/existing.mp4'}
            store.save(job)
            class FailedFramer:
                def analyze(self,*args,**kwargs): raise RuntimeError('fixture model unavailable')
            with self.assertRaisesRegex(RuntimeError,'fixture model unavailable'):
                reframe_job(store,job['id'],c['id'],framer=FailedFramer(),render=False)
            failed=store.load(job['id'])
            self.assertEqual(failed['clips'][0]['export'], {'media':'exports/existing.mp4'})
            self.assertEqual(failed['clips'][0]['framing'], c['framing'])
            server=ReviewServer(store,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:%d/api/jobs/%s/'%(server.server_port,job['id'])
            try:
                payload=json.dumps(dict(clip_id=c['id'],regions=regions,policy='auto')).encode()
                req=urllib.request.Request(base+'framing',data=payload,headers={'Content-Type':'application/json','X-Clip-Token':server.token})
                with urllib.request.urlopen(req) as response: saved=json.load(response)
                plan=saved['clips'][0]['framing']
                self.assertIsNone(saved['clips'][0]['export'])
                for path in (plan['preview'],plan['samples'][0]['file']):
                    with urllib.request.urlopen(base+path) as response:
                        self.assertEqual(response.headers['Content-Type'],'image/jpeg')
                for path in ('framing/abc123-r8/other.jpg','framing/abc123-r8/plan.json'):
                    with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(base+path)
                    self.assertEqual(error.exception.code,404)
            finally:
                server.shutdown();server.server_close();server.executor.shutdown(wait=True);thread.join()
