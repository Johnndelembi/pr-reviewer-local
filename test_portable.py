from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import ai_runtime
import check_sandbox
import reviewer
import runtime_config
import sms_notifier
import worker


class PortableTests(unittest.TestCase):
    def test_api_mode_sms_uses_controller_facts_without_a_codex_subprocess(self):
        config = {"ai": {"provider": "anthropic"}}
        fact = "repo#1: approved by your PR reviewer. https://github.com/owner/repo/pull/1"
        with patch.object(sms_notifier, "load_config", return_value=config), \
             patch.object(sms_notifier.subprocess, "Popen") as process:
            self.assertEqual(sms_notifier.generate_sms(Path("/data"), fact, "owner/repo", 1), fact)
        process.assert_not_called()

    def test_quoted_false_cannot_accidentally_enable_publishing_or_fixes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for section in (None, "autofix", "validation", "sms_notifications"):
                config = {"user": "reviewer", "repositories": ["owner/repo"], "model": "model",
                          "ai": {"provider": "openai"}}
                if section:
                    config[section] = {"enabled": "false"}
                else:
                    config["publish_reviews"] = "false"
                path.write_text(json.dumps(config))
                with self.subTest(section=section), self.assertRaisesRegex(ValueError, "not a quoted string"):
                    runtime_config.load_config(directory)

    def test_config_and_skill_changes_invalidate_deferred_context(self):
        pr = {"head": {"sha": "a"}, "base": {"sha": "b"}}
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "skill.md"
            skill.write_text("First rule")
            config = {"repositories": ["owner/repo"], "skill_files": [str(skill)], "ai": {"provider": "openai"}}
            with patch.object(reviewer, "CONFIG", config):
                before = reviewer.deferred_revision(pr)
                skill.write_text("Updated rule")
                self.assertNotEqual(before, reviewer.deferred_revision(pr))
                before = reviewer.deferred_revision(pr)
                config["ai"]["provider"] = "anthropic"
                self.assertNotEqual(before, reviewer.deferred_revision(pr))

    def test_configurable_poll_timezone_and_interval(self):
        config = {"schedule": {"timezone": "America/New_York", "interval_minutes": 15}}
        now = datetime(2026, 11, 1, 6, 37, tzinfo=timezone.utc)
        with patch.object(reviewer, "CONFIG", config):
            start, end = reviewer.poll_window(now)
        self.assertEqual(start.minute, 30)
        self.assertEqual(start.tzinfo.key, "America/New_York")
        self.assertEqual(end.timestamp() - start.timestamp(), 900)

    def test_secret_files_and_provider_url_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "key"
            secret.write_text("scoped-key\n")
            with patch.dict(os.environ, {"TEST_PROVIDER_FILE": str(secret)}):
                self.assertEqual(ai_runtime.credential("TEST_PROVIDER"), "scoped-key")
        for url in ("https://key@example.com/v1", "https://example.com/v1?key=secret", "file:///etc/passwd", "http://remote.example/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ai_runtime.provider_settings({"provider": "compatible", "base_url": url})

    def test_init_does_not_overwrite_an_installation_or_add_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), {"config_dir": directory, "user": "reviewer", "repo": ["owner/repo"],
                                    "model": "my-model", "provider": "anthropic", "base_url": None})()
            worker.initialize(args)
            with self.assertRaisesRegex(ValueError, "already exists"):
                worker.initialize(args)
            config = runtime_config.load_config(directory)
            self.assertEqual(config["ai"]["provider"], "anthropic")
            self.assertFalse(config["publish_reviews"])
            self.assertFalse(config["autofix"]["enabled"])
            self.assertNotIn("GH_TOKEN", json.dumps(config))
            self.assertEqual(config["model"], "my-model")

    def test_archive_transport_streams_owned_files_without_host_bind_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "source.py").write_text("value = 1\n")
            captured = {}
            def command(argv, *, input_file, **kwargs):
                captured["argv"] = argv
                with tarfile.open(fileobj=input_file, mode="r") as archive:
                    members = archive.getmembers()
                    captured["owners"] = {(item.uid, item.gid) for item in members}
                    captured["source"] = archive.extractfile("./source.py").read()
                return {"exit_code": 0, "timed_out": False}
            with patch.object(check_sandbox, "_command", side_effect=command):
                check_sandbox._copy_to_volume(source, ["docker"], "keeper", 30, 10000)
            self.assertEqual(captured["owners"], {(1000, 1000)})
            self.assertEqual(captured["source"], b"value = 1\n")
            self.assertEqual(captured["argv"], ["docker", "exec", "-i", "keeper", "tar", "-xf", "-", "-C", "/workspace"])


if __name__ == "__main__":
    unittest.main()
