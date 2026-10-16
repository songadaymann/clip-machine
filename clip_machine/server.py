"""Loopback-only review adapter. The core engine has no HTTP dependency."""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import shutil
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .demo import DemoProvider, create_demo
from .captions import STYLE_VERSION
from .engine import (analyze_job, caption_preview, create_job, edit_captions, edit_clip, export_job, reframe_job,
                     save_framing, suggest_caption_emphasis)
from .media import capabilities
from .storage import verify_source


MAX_UPLOAD = 50 * 1024 ** 3
WEB = Path(__file__).parent / "web"


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store, port):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.active = set()
        self.guard = threading.RLock()
        super().__init__(("127.0.0.1", port), Handler)

    def submit(self, job_id, operation):
        with self.guard:
            if job_id in self.active:
                raise ValueError("This episode is already processing")
            self.active.add(job_id)

        def work():
            try:
                operation()
            except Exception as error:
                # Also cover adapter failures before the engine starts a stage.
                job = self.store.load(job_id)
                if job["status"] != "failed":
                    job["error"] = str(error)
                    self.store.event(job, "failed", str(error))
            finally:
                with self.guard:
                    self.active.discard(job_id)
        self.executor.submit(work)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def job(self, identity):
        job = self.server.store.load(identity)
        with self.server.guard:
            job["busy"] = identity in self.server.active
        return job

    def do_GET(self):
        if not self.allowed_host():
            self.json({"error": "Invalid local host"}, 403)
            return
        try:
            path = urlparse(self.path).path
            if path == "/api/config":
                self.json({"token": self.server.token, "capabilities": capabilities(), "caption_style": STYLE_VERSION})
            elif path == "/api/jobs":
                self.json(self.server.store.list())
            elif path in ("/", "/app.js", "/style.css"):
                self.file(WEB / ("index.html" if path == "/" else path[1:]))
            else:
                match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})(?:/(transcript|energy|source|captions/[a-f0-9]{12}|files/[^/]+|framing/[a-f0-9]+-r[0-9]+/[a-z0-9-]+\.jpg))?", path)
                if not match:
                    self.json({"error": "Not found"}, 404)
                    return
                identity, action = match.groups()
                job = self.job(identity)
                if not action:
                    self.json(job)
                elif action in ("transcript", "energy"):
                    self.json(json.loads((self.server.store.folder(identity) / (action + ".json")).read_text()))
                elif action == "source":
                    self.file(verify_source(job))
                elif action.startswith("captions/"):
                    self.json(caption_preview(self.server.store, identity, action.split("/", 1)[1]))
                elif action.startswith("framing/"):
                    allowed = {s["file"] for c in job["clips"] for s in c.get("framing", {}).get("samples", [])}
                    allowed.update(c["framing"]["preview"] for c in job["clips"] if c.get("framing", {}).get("preview"))
                    if action not in allowed:
                        raise FileNotFoundError()
                    self.file(self.server.store.folder(identity) / action)
                else:
                    name = action.split("/", 1)[1]
                    allowed = {value for clip in job["clips"] if clip.get("export")
                               for key, value in clip["export"].items() if key in ("media", "srt", "vtt", "ass")}
                    relative = "exports/" + name
                    if relative not in allowed:
                        raise FileNotFoundError()
                    self.file(self.server.store.folder(identity) / relative)
        except (BrokenPipeError, ConnectionResetError):
            return
        except FileNotFoundError:
            self.json({"error": "Not available yet"}, 404)
        except (ValueError, KeyError, OSError) as error:
            self.json({"error": str(error)}, 400)

    def do_POST(self):
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if (not self.allowed_host() or self.headers.get("X-Clip-Token") != self.server.token
                or (origin and origin not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port))):
            self.json({"error": "Invalid local request"}, 403)
            return
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/upload":
                self.upload(parsed)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 2 * 1024 ** 2:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request must be an object")
            store = self.server.store
            if parsed.path == "/api/demo":
                job = create_demo(store)
                self.server.submit(job["id"], lambda: analyze_job(store, job["id"], DemoProvider()))
                self.json(self.job(job["id"]), 202)
            elif parsed.path == "/api/jobs":
                uploads = store.root.parent / "uploads"
                source = uploads / self.upload_name(body["source"])
                transcript = uploads / self.upload_name(body["transcript"]) if body.get("transcript") else None
                options = body.get("options", {})
                if not isinstance(options, dict) or set(options) - {"provider", "model", "direction", "count", "min_seconds", "max_seconds", "layout", "burn_captions", "auto_render", "smart_framing"}:
                    raise ValueError("Invalid processing options")
                if options.get("provider", "codex") not in ("codex", "claude"):
                    raise ValueError("Choose Codex or Claude")
                for key in ("burn_captions", "auto_render", "smart_framing"):
                    if key in options and type(options[key]) is not bool:
                        raise ValueError(key + " must be true or false")
                job = create_job(store, source, options, transcript)
                if isinstance(body.get("source_name"), str):
                    job["source_name"] = Path(body["source_name"]).name[:255]
                    store.save(job)
                self.server.submit(job["id"], lambda: analyze_job(store, job["id"]))
                self.json(self.job(job["id"]), 202)
            else:
                match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/(edit|export|retry|frame|framing|captions|emphasis)", parsed.path)
                if not match:
                    self.json({"error": "Not found"}, 404)
                    return
                identity, action = match.groups()
                with self.server.guard:
                    if identity in self.server.active:
                        raise ValueError("Wait for this episode to finish processing")
                    if action == "edit":
                        result = edit_clip(store, identity, body["clip_id"], body.get("start"), body.get("end"), body.get("decision"))
                        self.json(result)
                    elif action == "captions":
                        self.json(edit_captions(store, identity, body["clip_id"], body.get("changes", {})))
                    elif action == "emphasis":
                        job = store.load(identity)
                        provider = DemoProvider() if job["options"].get("provider") == "demo-fixture" else None
                        self.server.submit(identity, lambda: suggest_caption_emphasis(store, identity, body.get("clip_id"), provider))
                        self.json(self.job(identity), 202)
                    elif action == "framing":
                        self.json(save_framing(store, identity, body["clip_id"], body.get("regions", []), body.get("policy", "auto")))
                    elif action == "frame":
                        self.server.submit(identity, lambda: reframe_job(store, identity, body.get("clip_id"), body.get("policy", "auto")))
                        self.json(self.job(identity), 202)
                    elif action == "export":
                        job = store.load(identity)
                        if not job["clips"]:
                            raise ValueError("No clips to export")
                        if body.get("clip_id") and not any(c["id"] == body["clip_id"] for c in job["clips"]):
                            raise ValueError("Unknown clip")
                        self.server.submit(identity, lambda: export_job(store, identity, body.get("clip_id")))
                        self.json(self.job(identity), 202)
                    else:
                        job = store.load(identity)
                        if job["clips"]:
                            raise ValueError("Use export to retry rendering existing clips")
                        provider = DemoProvider() if job["options"].get("provider") == "demo-fixture" else None
                        self.server.submit(identity, lambda: analyze_job(store, identity, provider))
                        self.json(self.job(identity), 202)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            self.json({"error": str(error)}, 400)

    @staticmethod
    def upload_name(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}\.[a-z0-9]{1,8}", value):
            raise ValueError("Invalid upload ID")
        return value

    def upload(self, parsed):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError("Choose a file between 1 byte and 50 GB")
        filename = Path(parse_qs(parsed.query).get("name", ["episode.mp4"])[0]).name
        suffix = Path(filename).suffix.lower()
        if suffix not in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".aiff", ".aif", ".json"):
            raise ValueError("Choose audio, video, or transcript JSON")
        folder = self.server.store.root.parent / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(folder).free < length + 1024 ** 3:
            raise ValueError("Not enough free space for this upload and processing")
        identity = uuid.uuid4().hex + suffix
        target = folder / identity
        partial = target.with_suffix(target.suffix + ".part")
        try:
            with partial.open("wb") as output:
                remaining = length
                while remaining:
                    block = self.rfile.read(min(1024 ** 2, remaining))
                    if not block:
                        raise ValueError("Upload interrupted")
                    output.write(block)
                    remaining -= len(block)
            partial.rename(target)
        finally:
            partial.unlink(missing_ok=True)
        self.json({"id": identity, "name": filename}, 201)

    def file(self, path):
        path = Path(path)
        total = path.stat().st_size
        start, end = 0, total - 1
        range_header = self.headers.get("Range")
        partial = bool(range_header)
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                self.send_error(416)
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(end, int(last)) if last else end
            else:
                start = max(0, total - int(last))
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        mime = {".js": "text/javascript", ".vtt": "text/vtt", ".srt": "application/x-subrip"}.get(path.suffix,
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") else ""))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(path.name))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.end_headers()
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                block = source.read(min(256 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)


def serve(store, port=8787, open_browser=False):
    # A killed process cannot keep doing work. Make interrupted work retryable.
    for entry in store.list():
        job = store.load(entry["id"])
        if job["status"] not in ("ready", "complete", "failed"):
            job.update(status="failed", error="Processing was interrupted. Retry this episode.",
                       message="Processing interrupted")
            store.save(job)
    server = ReviewServer(store, port)
    url = "http://127.0.0.1:%d" % server.server_port
    print("Clip Machine: " + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        server.executor.shutdown(wait=True)
