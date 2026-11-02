import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from jsonschema import ValidationError
import ai_runtime as ai
import reviewer


RESULT = {"complete": False, "summary": "Inspected the change; additional evidence is needed.",
          "limitations": "Provider integration cannot be verified here.", "findings": [],
          "prior_finding_statuses": []}
SCHEMA = json.loads((Path(__file__).parent / "review-schema.json").read_text())


def response(name, args, identifier="call-1"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": identifier, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args)}}]}}]}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "head").mkdir()
        (self.root / "head/source.py").write_text("answer = 42\n")
        (self.root / "context.json").write_text('{"repository":"owner/repo"}')
        self.config = {"model": "chosen-model", "ai": {"provider": "openai", "api_key_env": "TEST_AI_KEY"}}
        env = patch.dict(os.environ, {"TEST_AI_KEY": "secret-not-for-tools"})
        env.start()
        self.addCleanup(env.stop)

    def test_openai_gemini_openrouter_ollama_and_custom_tool_roundtrip(self):
        for provider in ("openai", "gemini", "openrouter", "ollama", "compatible"):
            with self.subTest(provider=provider):
                self.config["ai"].update(provider=provider, base_url="https://gateway.example/v1")
                replies = [response("read_file", {"path": "head/source.py"}), response("finish", RESULT, "final")]
                with patch.object(ai, "request_json", side_effect=replies) as request:
                    result = ai.run_agent(self.config, "Review", SCHEMA, self.root, model="override-model")
                self.assertEqual(result, RESULT)
                url, headers, payload, _ = request.call_args.args
                self.assertEqual(url, "https://gateway.example/v1/chat/completions")
                self.assertEqual(payload["model"], "override-model")
                self.assertEqual(headers["Authorization"], "Bearer secret-not-for-tools")
                output = next(message for message in payload["messages"] if message["role"] == "tool")
                self.assertIn("answer = 42", output["content"])
                self.assertNotIn("secret-not-for-tools", json.dumps(payload))

    def test_anthropic_tool_result_protocol(self):
        self.config["ai"]["provider"] = "anthropic"
        replies = [
            {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "read", "name": "read_file", "input": {"path": "head/source.py"}}]},
            {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "final", "name": "finish", "input": RESULT}]},
        ]
        with patch.object(ai, "request_json", side_effect=replies) as request:
            self.assertEqual(ai.run_agent(self.config, "Review", SCHEMA, self.root), RESULT)
        url, headers, payload, _ = request.call_args.args
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "secret-not-for-tools")
        output = next(block for message in payload["messages"] for block in
                      (message["content"] if isinstance(message["content"], list) else [])
                      if block.get("type") == "tool_result")
        self.assertEqual(output["tool_use_id"], "read")
        self.assertIn("answer = 42", output["content"])

    def test_vertex_uses_google_cloud_adc_access_token(self):
        self.config["ai"] = {"provider": "vertex", "project_id": "my-google-project", "location": "global"}
        replies = [response("read_file", {"path": "head/source.py"}), response("finish", RESULT, "final")]
        with patch.object(ai, "vertex_credentials", return_value=object()) as credentials, \
                patch.object(ai, "vertex_access_token", side_effect=["access-token-1", "access-token-2"]) as access_token, \
                patch.object(ai, "request_json", side_effect=replies) as request:
            self.assertEqual(ai.run_agent(self.config, "Review", SCHEMA, self.root), RESULT)
        credentials.assert_called_once_with()
        self.assertEqual(access_token.call_count, 2)
        calls = request.call_args_list
        self.assertEqual(calls[0].args[0], "https://aiplatform.googleapis.com/v1/projects/my-google-project/locations/global/endpoints/openapi/chat/completions")
        self.assertEqual([call.args[1]["Authorization"] for call in calls],
                         ["Bearer access-token-1", "Bearer access-token-2"])

    def test_vertex_access_token_refreshes_expired_adc(self):
        class Credentials:
            valid = False
            token = None
            def refresh(self, request):
                self.token = "fresh-access-token"
        credentials = Credentials()
        with patch("google.auth.transport.requests.Request") as request:
            self.assertEqual(ai.vertex_access_token(credentials), "fresh-access-token")
        credentials.valid = True
        with patch("google.auth.transport.requests.Request") as request:
            self.assertEqual(ai.vertex_access_token(credentials), "fresh-access-token")
            request.assert_not_called()

    def test_invalid_or_truncated_output_cannot_be_accepted(self):
        for reply in (response("finish", {"complete": True}), {"choices": [{"finish_reason": "length"}]}):
            with self.subTest(reply=reply), patch.object(ai, "request_json", return_value=reply):
                with self.assertRaises((ValidationError, RuntimeError)):
                    ai.run_agent(self.config, "Review", SCHEMA, self.root)

    def test_unknown_shell_tool_is_rejected(self):
        with patch.object(ai, "request_json", return_value=response("shell", {"command": "env"})):
            with self.assertRaisesRegex(ValueError, "unknown tool"):
                ai.run_agent(self.config, "Review", SCHEMA, self.root)

    def test_no_completion_after_budget_exhaustion(self):
        self.config["ai"]["max_turns"] = 1
        with patch.object(ai, "request_json", return_value=response("read_file", {"path": "context.json"})):
            with self.assertRaisesRegex(RuntimeError, "turn budget"):
                ai.run_agent(self.config, "Review", SCHEMA, self.root)

    def test_finish_cannot_hide_other_file_operations(self):
        reply = response("finish", RESULT)
        reply["choices"][0]["message"]["tool_calls"] += response("delete_file", {"path": "head/source.py"})["choices"][0]["message"]["tool_calls"]
        with patch.object(ai, "request_json", return_value=reply):
            with self.assertRaisesRegex(ValueError, "only tool call"):
                ai.run_agent(self.config, "Fix", SCHEMA, self.root, writable=self.root / "head")
        self.assertTrue((self.root / "head/source.py").exists())

    def test_path_traversal_symlinks_and_metadata_are_inaccessible(self):
        files = ai.Workspace(self.root, self.root / "head")
        (self.root / "head/link").symlink_to("/etc")
        for path in ("../config.json", "/etc/passwd", "head/link/passwd", "head/.git/config", "head/.codex/config.toml"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                files.call("read_file", {"path": path})
        with self.assertRaises(ValueError):
            files.call("write_file", {"path": "context.json", "content": "changed"})
        self.assertEqual((self.root / "context.json").read_text(), '{"repository":"owner/repo"}')

    def test_review_tools_cannot_write_and_fix_tools_only_change_head(self):
        with self.assertRaises(ValueError):
            ai.Workspace(self.root).call("write_file", {"path": "head/source.py", "content": "bad"})
        files = ai.Workspace(self.root, self.root / "head")
        files.call("write_file", {"path": "head/new.py", "content": "new = 1\n"})
        self.assertEqual((self.root / "head/new.py").read_text(), "new = 1\n")
        files.call("delete_file", {"path": "head/new.py"})
        self.assertFalse((self.root / "head/new.py").exists())

    def test_reads_and_searches_are_paginated(self):
        (self.root / "head/long.py").write_text("\n".join(f"value {i}" for i in range(450)))
        files = ai.Workspace(self.root)
        page = files.call("read_file", {"path": "head/long.py", "line_count": 10})
        self.assertEqual((len(page["lines"]), page["next_line"]), (10, 11))
        matches = files.call("search_text", {"path": "head", "query": "value"})
        self.assertEqual((len(matches["matches"]), matches["next_offset"]), (100, 100))

    def test_http_redirect_does_not_forward_credentials(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                ai.request_json(f"http://127.0.0.1:{server.server_port}/messages", {"Authorization": "Bearer test-secret"}, {}, 3)
            self.assertEqual(seen, ["/messages"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_controller_retains_policy_and_schema_with_api_provider(self):
        (self.root / "work").mkdir()
        config = {**self.config, "user": "reviewer", "reasoning": "high", "skill_files": [], "validation": {"enabled": False}}
        context = {"review_history": {"round": 1, "prior_findings": []},
                   "companion_evidence": {"pull_requests": [], "unavailable": []}}
        with patch.object(reviewer, "ROOT", self.root), patch.object(reviewer, "CONFIG", config), \
             patch.object(reviewer, "prepare_context", return_value=context), \
             patch.object(reviewer, "run_checks", return_value={"status": "skipped", "enabled": False}), \
             patch.object(ai, "request_json", return_value=response("finish", RESULT)), \
             patch.object(reviewer, "command") as command:
            result = reviewer.evaluate("owner/repo", 1, {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}, [])
        command.assert_not_called()
        self.assertFalse(result["complete"])
        self.assertIsNone(reviewer.review_body(result, "a" * 40))
        self.assertEqual(json.loads((self.root / "last-review.json").read_text())["model"], "chosen-model")


if __name__ == "__main__":
    unittest.main()
