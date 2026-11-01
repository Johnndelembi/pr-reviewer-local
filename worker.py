#!/usr/bin/env python3
"""Container entry point: initialize, inspect, review once, or poll continuously."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from runtime_config import load_config


ROOT = Path(__file__).parent
STOP = threading.Event()


def initialize(args):
    destination = Path(args.config_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "config.json"
    if path.exists():
        raise ValueError("config.json already exists; edit it instead of overwriting it")
    config = json.loads((ROOT / "config.example.json").read_text())
    config.update(user=args.user, repositories=args.repo, model=args.model)
    config["ai"]["provider"] = args.provider
    if args.base_url:
        config["ai"]["base_url"] = args.base_url
    config["skill_files"] = [str(destination / "skills/review.md")]
    config["autofix"]["skill_files"] = [str(destination / "skills/review.md")]
    config["autofix"]["commit_email"] = args.user + "@users.noreply.github.com"
    skills = destination / "skills"
    skills.mkdir(exist_ok=True)
    if not (skills / "review.md").exists():
        (skills / "review.md").write_text((ROOT / "skills/review.md").read_text())
    # Credentials belong in environment variables or secrets, never this public configuration.
    path.write_text(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)
    print("Created configuration and default skill. Set credentials, edit repositories/models, then run doctor and check.")


def stop(signum, frame):
    STOP.set()


def run_child(script, args):
    child = subprocess.Popen([sys.executable, "-B", str(ROOT / script), *args], start_new_session=True)
    while child.poll() is None:
        if STOP.wait(0.25):
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return child.returncode


def doctor(config):
    from ai_runtime import credential, provider_settings
    provider, endpoint, key_name = provider_settings(config["ai"])
    gh_token = credential("GH_TOKEN") or credential("GITHUB_TOKEN")
    if not gh_token:
        raise ValueError("Set GH_TOKEN or GH_TOKEN_FILE for the configured GitHub account")
    if key_name and not credential(key_name):
        raise ValueError(f"Set {key_name} or {key_name}_FILE")
    print(json.dumps({"status": "ready", "user": config["user"], "repositories": config["repositories"],
                      "provider": provider, "base_url": endpoint, "model": config["model"],
                      "publish_reviews": bool(config.get("publish_reviews", False)),
                      "autofix": bool(config.get("autofix", {}).get("enabled")),
                      "validation": bool(config.get("validation", {}).get("enabled")),
                      "note": "Local configuration check only; credentials and model capabilities are not verified remotely."}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create configuration without overwriting an existing install")
    init.add_argument("--config-dir", default="/config")
    init.add_argument("--user", required=True)
    init.add_argument("--repo", action="append", required=True)
    init.add_argument("--provider", choices=("openai", "anthropic", "gemini", "openrouter", "ollama", "compatible"), default="openai")
    init.add_argument("--model", required=True)
    init.add_argument("--base-url")
    sub.add_parser("doctor", help="Validate local configuration and credential presence; never sends tokens to the model")
    sub.add_parser("check", help="Discover requested PRs without AI calls or GitHub writes")
    sub.add_parser("serve", help="Poll continuously; publishing and auto-fixing require explicit configuration")
    for name in ("review", "autofix"):
        action = sub.add_parser(name, help="Run once; preview unless publishing is explicitly enabled")
        action.add_argument("--dry-run", action="store_true")
        action.add_argument("--pr")
        if name == "review":
            action.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "init":
        initialize(args)
        return 0
    data = Path(os.environ.get("REVIEWER_DATA_DIR", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    config = load_config(data)
    if "ai" not in config:
        raise ValueError("The Docker image requires ai configuration; Codex CLI authentication is supported only by the legacy local runner")
    # gh understands GH_TOKEN; never persist a login or include credentials in process arguments.
    if not os.environ.get("GH_TOKEN"):
        from ai_runtime import credential
        token = credential("GH_TOKEN") or credential("GITHUB_TOKEN")
        if token:
            os.environ["GH_TOKEN"] = token
    if args.command == "doctor":
        doctor(config)
        return 0
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.command == "check":
        return run_child("reviewer.py", ["--check"])
    if args.command in ("review", "autofix"):
        options = ["--pr", args.pr] if args.pr else []
        if args.command == "review":
            options += ["--limit", str(args.limit)]
            if args.dry_run or not config.get("publish_reviews", False):
                options.append("--dry-run")
        elif args.dry_run or not config.get("autofix", {}).get("enabled", False):
            options.append("--dry-run")
        return run_child("reviewer.py" if args.command == "review" else "autofixer.py", options)
    print("Reviewer service started. Review publishing and auto-fix follow the mounted configuration.", flush=True)
    while not STOP.is_set():
        config = load_config(data)
        review_args = [] if config.get("publish_reviews", False) else ["--check"]
        code = run_child("reviewer.py", review_args)
        if code:
            print(f"Reviewer poll exited {code}; retry on a later poll.", file=sys.stderr, flush=True)
        if config.get("autofix", {}).get("enabled", False) and not STOP.is_set():
            code = run_child("autofixer.py", [])
            if code:
                print(f"Auto-fix poll exited {code}; retry on a later poll.", file=sys.stderr, flush=True)
        # Wake frequently so configured poll windows, signals, and configuration edits take effect.
        STOP.wait(60 if config.get("publish_reviews") or config.get("autofix", {}).get("enabled")
                  else config.get("schedule", {}).get("interval_minutes", 10) * 60)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        raise SystemExit(2)
