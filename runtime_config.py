"""Portable configuration and packaged asset paths."""
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def asset(root, name):
    local = Path(root) / name
    return local if local.is_file() else Path(__file__).parent / name


def load_config(root):
    path = Path(os.environ.get("REVIEWER_CONFIG", str(Path(root) / "config.json")))
    config = json.loads(path.read_text())
    config.setdefault("gh", "gh")
    config.setdefault("reasoning", "high")
    config.setdefault("skill_files", [])
    config.setdefault("validation", {"enabled": False})
    if "ai" in config:
        for section in ("ai", "autofix", "validation", "schedule", "sms_notifications"):
            if section in config and not isinstance(config[section], dict):
                raise ValueError(f"{section} must be a JSON object")
        for owner, field, label in [(config, "publish_reviews", "publish_reviews"),
                                    (config, "draft_at_round_six", "draft_at_round_six"),
                                    *[(config.get(section, {}), "enabled", section + ".enabled")
                                      for section in ("autofix", "validation", "sms_notifications")]]:
            if field in owner and type(owner[field]) is not bool:
                raise ValueError(f"{label} must be JSON true or false, not a quoted string")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", config.get("user", "")):
            raise ValueError("Configure a valid GitHub user")
        repos = config.get("repositories")
        if (not isinstance(repos, list) or not repos or
                any(not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
                    for repo in repos)):
            raise ValueError("Configure an explicit owner/repository allowlist")
        if not isinstance(config.get("model"), str) or not config["model"].strip():
            raise ValueError("Configure an AI model ID")
        from ai_runtime import provider_settings
        provider_settings(config["ai"])
        for skill in [*config["skill_files"], *config.get("autofix", {}).get("skill_files", [])]:
            if not isinstance(skill, str) or not Path(skill).is_file():
                raise ValueError("Configured skill file is unavailable")
        schedule = config.get("schedule", {})
        ZoneInfo(schedule.get("timezone", "UTC"))
        for key in ("interval_minutes", "daytime_interval_minutes"):
            if key in schedule and (type(schedule[key]) is not int or not 1 <= schedule[key] <= 1440):
                raise ValueError(f"schedule.{key} must be an integer from 1 to 1440")
        for key in ("daytime_start_hour", "daytime_end_hour"):
            if key in schedule and (type(schedule[key]) is not int or not 0 <= schedule[key] <= 23):
                raise ValueError(f"schedule.{key} must be an hour from 0 to 23")
        if config.get("autofix", {}).get("enabled") and not config["autofix"].get("commit_email"):
            raise ValueError("Auto-fix requires autofix.commit_email")
    return config
