"""The CLI is a thin adapter over the same engine used by the local web server."""
import argparse
import json
import sys

from .demo import DemoProvider, create_demo
from .engine import analyze_job, create_job, export_job
from .media import capabilities
from .storage import JobStore


def main():
    parser = argparse.ArgumentParser(description="Modular, local-first podcast clipping")
    parser.add_argument("--data-dir", default="data/jobs", help="Private job storage (default: data/jobs)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check installed processing tools")
    serve = commands.add_parser("serve", help="Open the local review app")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--open", action="store_true")
    demo = commands.add_parser("demo", help="Create synthetic media and sample rankings (no AI calls)")
    demo.add_argument("--no-render", action="store_true")
    process = commands.add_parser("process", help="Analyze an episode")
    process.add_argument("source")
    process.add_argument("--transcript", help="Existing Whisper/WhisperX JSON")
    process.add_argument("--provider", choices=["codex", "claude"], default="codex")
    process.add_argument("--model", help="Use a specific model available to the provider CLI")
    process.add_argument("--direction", default="")
    process.add_argument("--count", type=int, default=6)
    process.add_argument("--min-seconds", type=float, default=20)
    process.add_argument("--max-seconds", type=float, default=90)
    process.add_argument("--layout", choices=["original", "portrait"], default="original")
    process.add_argument("--render", action="store_true")
    process.add_argument("--no-captions", action="store_true")
    process.add_argument("--transcription-model", default="mlx-community/whisper-large-v3-turbo")
    retry = commands.add_parser("retry", help="Retry a failed analysis, preserving cached model responses")
    retry.add_argument("job_id")
    export = commands.add_parser("export", help="Export all non-rejected clips, or a single clip")
    export.add_argument("job_id")
    export.add_argument("--clip", dest="clip_id")
    show = commands.add_parser("show", help="Print a job's versioned JSON manifest")
    show.add_argument("job_id")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            print(json.dumps(capabilities(), indent=2))
            return
        store = JobStore(args.data_dir)
        if args.command == "serve":
            from .server import serve
            serve(store, args.port, args.open)
            return
        if args.command == "demo":
            job = create_demo(store)
            if args.no_render:
                job["options"]["auto_render"] = False
                store.save(job)
            result = analyze_job(store, job["id"], DemoProvider())
        elif args.command == "process":
            options = {"provider": args.provider, "model": args.model, "direction": args.direction,
                       "count": args.count, "min_seconds": args.min_seconds, "max_seconds": args.max_seconds,
                       "layout": args.layout, "auto_render": args.render, "burn_captions": not args.no_captions,
                       "transcription_model": args.transcription_model}
            job = create_job(store, args.source, options, args.transcript)
            print("Job: " + job["id"], file=sys.stderr, flush=True)
            result = analyze_job(store, job["id"])
        elif args.command == "retry":
            job = store.load(args.job_id)
            result = analyze_job(store, args.job_id, DemoProvider() if job["options"].get("provider") == "demo-fixture" else None)
        elif args.command == "export":
            result = export_job(store, args.job_id, args.clip_id)
        else:
            result = store.load(args.job_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print("Clip Machine: " + str(error), file=sys.stderr)
        raise SystemExit(1)
