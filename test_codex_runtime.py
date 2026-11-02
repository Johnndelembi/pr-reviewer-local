import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ai_runtime
import codex_runtime as codex
import runtime_config
import worker


class CodexTests(unittest.TestCase):
    def test_browser_init_needs_no_ai_key_and_preserves_api_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), dict(config_dir=directory, user="reviewer", repo=["owner/repo"],
                                         model="chosen-model", provider="codex", base_url=None))()
            worker.initialize(args)
            config = runtime_config.load_config(directory)
            self.assertEqual(config["ai"]["provider"], "codex")
            self.assertNotIn("api_key_env", config["ai"])
            self.assertEqual(ai_runtime.provider_settings(config["ai"])[2], "")
            self.assertFalse(config["publish_reviews"])
            for settings in ({"provider": "codex", "api_key_env": "AI_API_KEY"},
                             {"provider": "codex", "base_url": "https://example.com"}):
                with self.assertRaises(ValueError):
                    ai_runtime.provider_settings(settings)

    def test_login_is_device_auth_without_controller_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "CODEX_HOME": directory, "GH_TOKEN": "github-secret", "AI_API_KEY": "ai-secret",
            "NOTIFY_API_KEY": "sms-secret", "OPENAI_BASE_URL": "https://untrusted.example"}), \
                patch.object(codex.subprocess, "call", return_value=0) as call:
            self.assertEqual(codex.auth("login"), 0)
            command = call.call_args.args[0]
            self.assertEqual(command[-2:], ["login", "--device-auth"])
            self.assertIn('forced_login_method="chatgpt"', command)
            env = call.call_args.kwargs["env"]
            self.assertEqual(env["CODEX_HOME"], directory)
            self.assertTrue(all(key not in env for key in ["GH_TOKEN", "AI_API_KEY", "NOTIFY_API_KEY", "OPENAI_BASE_URL"]))
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)

    def test_doctor_requires_a_valid_codex_session_but_not_an_ai_key(self):
        config = {"user": "reviewer", "repositories": ["owner/repo"], "model": "model", "ai": {"provider": "codex"}}
        with patch.dict(os.environ, {"GH_TOKEN": "scoped-token"}), patch.object(codex, "auth", return_value=1):
            with self.assertRaisesRegex(ValueError, "signed out"):
                worker.doctor(config)
        with patch.dict(os.environ, {"GH_TOKEN": "scoped-token"}), patch.object(codex, "auth", return_value=0), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            worker.doctor(config)
            self.assertEqual(json.loads(output.getvalue())["provider"], "codex")

    def test_vertex_init_and_doctor_use_adc_without_an_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            args = type("Args", (), dict(config_dir=directory, user="reviewer", repo=["owner/repo"],
                                         model="google/gemini-3.5-flash", provider="vertex", base_url=None,
                                         project="my-google-project", location="global"))()
            worker.initialize(args)
            config = runtime_config.load_config(directory)
            self.assertEqual(config["ai"]["provider"], "vertex")
            self.assertNotIn("api_key_env", config["ai"])
            with patch.dict(os.environ, {"GH_TOKEN": "scoped-token"}), \
                    patch.object(ai_runtime, "vertex_credentials", return_value=object()), \
                    patch.object(ai_runtime, "vertex_access_token", return_value="access-token"), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                worker.doctor(config)
            self.assertEqual(json.loads(output.getvalue())["provider"], "vertex")

    def test_mcp_tools_enforce_the_existing_file_boundary_and_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "head").mkdir(); (root / "head/a.py").write_text("value = 1\n")
            (root / "head/link").symlink_to(root / "head/a.py")
            requests = [
                {"id": 0, "method": "initialize"},
                {"id": 1, "method": "tools/list"},
                *[{"id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
                  for i, (name, arguments) in enumerate([
                      ("read_file", {"path": "head/a.py"}),
                      ("read_file", {"path": "../auth.json"}),
                      ("read_file", {"path": "head/link"}),
                      ("write_file", {"path": "context.json", "content": "bad"}),
                      ("write_file", {"path": "head/a.py", "content": "value = 2\n"}),
                      ("read_file", {"path": "head/a.py"})], 2)]]
            with patch("sys.stdin", io.StringIO("\n".join(map(json.dumps, requests)))), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                codex.serve_tools(root, root / "head", 5)
            responses = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
            self.assertEqual([t["name"] for t in responses[1]["tools"]],
                             ["list_files", "read_file", "search_text", "write_file", "delete_file"])
            self.assertFalse(responses[2]["isError"])
            self.assertTrue(all(responses[i]["isError"] for i in [3, 4, 5, 7]))
            self.assertEqual((root / "head/a.py").read_text(), "value = 2\n")
            self.assertFalse((root / "context.json").exists())

    def test_runner_preserves_schema_and_only_exposes_restricted_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "auth.json").write_text("{}")
            schema = {"type": "object", "properties": {"complete": {"type": "boolean"}}, "required": ["complete"]}
            config = {"model": "chosen-model", "reasoning": "high", "ai": {"provider": "codex"}}
            captured = {}
            def launch(command, **kwargs):
                captured.update(command=command, **kwargs)
                self.assertEqual({p.name for p in Path(kwargs["cwd"]).iterdir()}, {"schema.json", "models.json"})
                process = type("Process", (), {"pid": 99999999, "returncode": 0,
                    "communicate": lambda _, *a, **k: Path(command[command.index("--output-last-message") + 1]).write_text('{"complete":true}'),
                    "wait": lambda _: None})()
                return process
            with patch.dict(os.environ, {"CODEX_HOME": directory, "GH_TOKEN": "must-not-pass"}), \
                    patch.object(codex.subprocess, "Popen", side_effect=launch), patch.object(codex.os, "killpg"):
                result = ai_runtime.run_agent(config, "Review", schema, root)
            self.assertEqual(result, {"complete": True})
            command = captured["command"]
            for option in ["--ephemeral", "read-only",
                           "features.shell_tool=false", "features.view_image=false", 'web_search="disabled"']:
                self.assertIn(option, command)
            self.assertNotIn("GH_TOKEN", captured["env"])
            self.assertIn('model_reasoning_effort="high"', command)

    def test_signed_out_and_cli_failure_cannot_become_a_review(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODEX_HOME": directory}):
            config = {"ai": {"provider": "codex"}, "model": "model"}
            with self.assertRaisesRegex(ValueError, "signed out"):
                codex.run_agent(config, "Review", {}, directory)
            (Path(directory) / "auth.json").write_text("{}")
            process = type("Process", (), {"pid": 99999999, "returncode": 1,
                                          "communicate": lambda *a, **k: None, "wait": lambda _: None})()
            with patch.object(codex.subprocess, "Popen", return_value=process), patch.object(codex.os, "killpg"):
                with self.assertRaisesRegex(RuntimeError, "No result accepted"):
                    codex.run_agent(config, "Review", {}, directory)

    def test_bad_schema_budget_and_timeout_results_are_rejected_and_children_stopped(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODEX_HOME": directory}):
            (Path(directory) / "auth.json").write_text("{}")
            config = {"ai": {"provider": "codex"}, "model": "model"}
            schema = {"type": "object", "properties": {"complete": {"type": "boolean"}}, "required": ["complete"]}
            for failure in ("schema", "budget", "timeout"):
                def launch(command, **kwargs):
                    output = Path(command[command.index("--output-last-message") + 1])
                    def communicate(*args, **kwargs):
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired("codex", 1)
                        output.write_text('{"complete":"invalid"}' if failure == "schema" else '{"complete":true}')
                        if failure == "budget":
                            (output.parent / "budget-exhausted").touch()
                    return type("Process", (), {"pid": 99999999, "returncode": 0,
                                                 "communicate": communicate, "wait": lambda _: None})()
                with self.subTest(failure=failure), patch.object(codex.subprocess, "Popen", side_effect=launch), \
                        patch.object(codex.os, "killpg") as kill, self.assertRaises(Exception):
                    codex.run_agent(config, "Review", schema, directory)
                kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
