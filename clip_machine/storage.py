"""Filesystem job store. A studio can replace this without changing the engine."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    os.replace(str(temporary), str(path))


class JobStore:
    def __init__(self, root="data/jobs"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def folder(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid job ID")
        return self.root / job_id

    def create(self, source, options):
        source = Path(source).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Choose a media file")
        job = {"schema_version": 1, "id": uuid.uuid4().hex, "created_at": now(),
               "updated_at": now(), "status": "queued", "message": "Queued",
               "source": str(source), "source_name": source.name,
               "source_size": source.stat().st_size,
               "source_mtime_ns": source.stat().st_mtime_ns,
               "options": options, "clips": [], "rejected_candidates": [],
               "events": [], "error": None}
        self.save(job)
        return job

    def save(self, job):
        with self.lock:
            job["updated_at"] = now()
            write_json(self.folder(job["id"]) / "job.json", job)

    def load(self, job_id):
        with self.lock:
            return json.loads((self.folder(job_id) / "job.json").read_text())

    def list(self):
        jobs = []
        for path in self.root.glob("*/job.json"):
            try:
                job = self.load(path.parent.name)
                jobs.append({k: job[k] for k in ("id", "source_name", "status", "created_at")})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(jobs, key=lambda job: job["created_at"], reverse=True)

    def event(self, job, status, message):
        job.update(status=status, message=message)
        job["events"].append({"at": now(), "status": status, "message": message})
        self.save(job)


def verify_source(job):
    source = Path(job["source"])
    stat = source.stat()
    if stat.st_size != job["source_size"] or stat.st_mtime_ns != job["source_mtime_ns"]:
        raise ValueError("The source file changed. Create a new job so timestamps stay correct.")
    return source
