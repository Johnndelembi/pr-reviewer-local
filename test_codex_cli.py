"""Exercise the actual pinned CLI + MCP transport against a synthetic Responses server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_runtime


@unittest.skipUnless(shutil.which("codex"), "Codex CLI is required; Docker tests include it")
class CodexCLITests(unittest.TestCase):
    def test_real_cli_reads_and_writes_only_through_bounded_tools(self):
        requests = []
        result = {"summary": "Synthetic review complete"}
        errors = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    requests.append(payload)
                    if "tools" not in payload:
                        raise AssertionError("Unexpected request " + self.path + ": " + str(list(payload)))
                    tools = [tool for tool in payload["tools"] if "reviewer" in tool.get("name", "")]
                    stage = len(requests)
                    names = {tool.get("name") for tool in payload["tools"]}
                    dangerous = [name for name in names if name and not ("reviewer" in name or name in ("update_plan", "view_image", "request_user_input", "list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"))]
                    if dangerous:
                        raise AssertionError("Unexpected model tools: " + str(dangerous))
                    if stage == 1:
                        output = {"type": "function_call", "id": "fc_denied", "call_id": "call_denied", "name": "view_image",
                                  "arguments": json.dumps({"path": "/codex/auth.json"})}
                    elif stage == 2:
                        if "view_image is not allowed" not in json.dumps(payload["input"]):
                            raise AssertionError("Built-in image access was not denied")
                        name = next(tool["name"] for tool in tools if tool["name"].endswith("read_file"))
                        output = {"type": "function_call", "id": "fc_read", "call_id": "call_read", "name": name,
                                  "arguments": json.dumps({"path": "head/a.py"})}
                    elif stage == 3:
                        if "value = 1" not in json.dumps(payload["input"]):
                            raise AssertionError("MCP read result did not reach Codex")
                        name = next(tool["name"] for tool in tools if tool["name"].endswith("write_file"))
                        output = {"type": "function_call", "id": "fc_write", "call_id": "call_write", "name": name,
                                  "arguments": json.dumps({"path": "head/a.py", "content": "value = 2\n"})}
                    else:
                        output = {"type": "message", "id": "msg_final", "role": "assistant", "status": "completed",
                                  "content": [{"type": "output_text", "text": json.dumps(result), "annotations": []}]}
                    response = {"id": "resp_" + str(stage), "object": "response", "status": "completed", "output": [output],
                                "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}
                    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                              {"type": "response.output_item.added", "output_index": 0, "item": output},
                              {"type": "response.output_item.done", "output_index": 0, "item": output},
                              {"type": "response.completed", "response": response}]
                    body = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    errors.append(str(error))
                    self.send_error(400)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        launch = subprocess.Popen
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); workspace = root / "source"; workspace.mkdir()
                (workspace / "head").mkdir(); (workspace / "head/a.py").write_text("value = 1\n")
                home = root / "login"; home.mkdir(); (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "synthetic-test-key"}))
                diagnostics = root / "cli.log"
                def synthetic_launch(command, **kwargs):
                    # Only the auth/model transport is replaced; all production tool restrictions remain.
                    command = command[:-1] + ["-c", 'forced_login_method="api"', "-c", 'model_provider="synthetic"',
                        "-c", 'model_providers.synthetic.name="Synthetic test server"',
                        "-c", 'model_providers.synthetic.base_url="http://127.0.0.1:' + str(server.server_port) + '"',
                        "-c", 'model_providers.synthetic.env_key="MOCK_AI_KEY"',
                        "-c", 'model_providers.synthetic.wire_api="responses"', "-"]
                    kwargs["env"]["MOCK_AI_KEY"] = "synthetic-test-key"
                    with diagnostics.open("w") as output:
                        kwargs.update(stdout=output, stderr=output)
                        return launch(command, **kwargs)
                with patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
                        patch.object(codex_runtime.subprocess, "Popen", side_effect=synthetic_launch):
                    try:
                        actual = codex_runtime.run_agent({"model": "gpt-6.1-sol", "ai": {"provider": "codex", "job_timeout_seconds": 60}},
                            "Inspect head/a.py and correct the value using only MCP.",
                            {"type": "object", "properties": {"summary": {"type": "string"}},
                             "required": ["summary"], "additionalProperties": False}, workspace, writable=workspace / "head")
                    except Exception:
                        self.fail("Synthetic CLI test failed: " + str(errors) + "\n" + diagnostics.read_text()[-12000:])
                self.assertEqual(actual, result)
                self.assertEqual((workspace / "head/a.py").read_text(), "value = 2\n")
                self.assertFalse(errors)
                self.assertEqual(len(requests), 4)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
