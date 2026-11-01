import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from check_sandbox import _command, run_sandbox


def completed(exit_code=0, output="", timed_out=False):
    return {"exit_code": exit_code, "output": output, "timed_out": timed_out,
            "output_truncated": False, "duration_seconds": 0.1}


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name).resolve() / "head"
        self.manifests = Path(self.temporary.name).resolve() / "manifests"
        self.source.mkdir()
        self.manifests.mkdir()
        self.stages = [
            {"name": "install", "argv": ["npm", "ci", "--ignore-scripts"], "network": True},
            {"name": "build", "argv": ["npm", "run", "build"], "cwd": "/workspace/service"},
            {"name": "tests", "argv": ["npm", "test"], "cwd": "service"},
        ]

    def run_checks(self, **kwargs):
        return run_sandbox(self.source, self.manifests, kwargs.pop("stages", self.stages),
                           kwargs.pop("image", "reviewer-node:trusted"), kwargs.pop("settings", {}),
                           logging.getLogger("sandbox-test"), **kwargs)

    @patch("check_sandbox._command", return_value=completed())
    def test_install_has_only_manifests_and_checks_are_offline(self, command):
        report = self.run_checks()
        self.assertEqual(report["status"], "passed")
        runs = [call.args[0] for call in command.call_args_list
                if "run" in call.args[0] and "--detach" not in call.args[0]]
        self.assertEqual(len(runs), 3)
        install, build, tests = runs
        self.assertIn(f"type=bind,source={self.manifests},target=/input,readonly", install)
        self.assertFalse(any(str(self.source) in arg for arg in install))
        self.assertEqual(install[install.index("--network") + 1], "bridge")
        for argv in (build, tests):
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertIn(f"type=bind,source={self.source},target=/input,readonly", argv)
        self.assertIn('cp -R /input/. /workspace/', build[build.index("-c") + 1])
        self.assertNotIn('cp -R', tests[tests.index("-c") + 1])
        self.assertEqual(build[-5:], ["codex-check", "/workspace/service", "npm", "run", "build"])
        for argv in runs:
            for flag in ("--read-only", "--cap-drop", "--security-opt", "--pids-limit",
                         "--cpus", "--memory", "--memory-swap", "--tmpfs"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--user") + 1], "1000:1000")
            self.assertEqual(argv[argv.index("--pull") + 1], "never")
            self.assertNotIn("/var/run/docker.sock", " ".join(argv))
            self.assertIn("HTTP_PROXY=", argv)
            self.assertIn("http_proxy=", argv)
        self.assertEqual(sum("rm" in call.args[0] and "volume" not in call.args[0]
                             for call in command.call_args_list), 4)
        self.assertIn("o=size=3g,uid=1000,gid=1000,mode=0755,nosuid,nodev",
                      command.call_args_list[0].args[0])
        keeper = command.call_args_list[1].args[0]
        self.assertIn("--detach", keeper)
        self.assertEqual(keeper[keeper.index("--network") + 1], "none")
        self.assertFalse(any("type=bind" in arg for arg in keeper))
        self.assertTrue(command.call_args_list[-2].args[0][-1].endswith("-keeper"))
        self.assertEqual(command.call_args_list[-1].args[0][-4:-1], ["volume", "rm", "--force"])

    @patch("check_sandbox._command")
    def test_failed_stage_stops_checks_and_cleans_up(self, command):
        command.side_effect = [completed(), completed(), completed(1, "compile error"),
                               completed(), completed(), completed()]
        report = self.run_checks(stages=self.stages[1:])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["stages"]), 1)
        self.assertEqual(report["stages"][0]["output"], "compile error")
        self.assertEqual(command.call_count, 6)
        self.assertIn("rm", command.call_args_list[-2].args[0])
        self.assertIn("volume", command.call_args_list[-1].args[0])

    @patch("check_sandbox._command")
    def test_timeout_removes_daemon_container_then_volume(self, command):
        command.side_effect = [completed(), completed(), completed(-9, "partial", timed_out=True),
                               completed(), completed(), completed()]
        report = self.run_checks(stages=self.stages[1:],
                                 settings={"timeout_seconds": 999999, "output_limit_bytes": 99999999})
        self.assertEqual(report["status"], "timeout")
        self.assertEqual(command.call_args_list[2].kwargs["timeout"], 900)
        self.assertEqual(command.call_args_list[2].kwargs["output_limit"], 1_000_000)
        self.assertIn("--force", command.call_args_list[3].args[0])

    @patch("check_sandbox._command")
    def test_docker_failure_is_blocked_and_cleanup_errors_are_reported(self, command):
        command.side_effect = [completed(), completed(), completed(125, "image missing"),
                               completed(1, "daemon unavailable"), completed(), completed(1, "busy volume")]
        report = self.run_checks()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(len(report["cleanup_errors"]), 2)

    @patch("check_sandbox._command")
    def test_dependency_failure_is_setup_blocked_not_source_failure(self, command):
        command.side_effect = [completed(), completed(), completed(1, "registry unavailable"),
                               completed(), completed(), completed()]
        report = self.run_checks()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["stages"][0]["exit_code"], 1)

    @patch("check_sandbox.time.monotonic")
    @patch("check_sandbox._command")
    def test_total_job_budget_stops_before_another_stage(self, command, monotonic):
        monotonic.side_effect = [0, 2]
        command.return_value = completed()
        report = self.run_checks(settings={"job_timeout_seconds": 1})
        self.assertEqual(report["status"], "timeout")
        self.assertEqual(report["stages"], [])
        self.assertIn("total time budget", report["reason"])

    @patch("check_sandbox._command")
    def test_missing_docker_binary_is_blocked(self, command):
        command.side_effect = FileNotFoundError("docker unavailable")
        report = self.run_checks()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("unavailable", report["reason"])

    @patch("check_sandbox._command", return_value=completed())
    def test_no_network_stage_can_run_after_source_is_copied(self, command):
        with self.assertRaisesRegex(ValueError, "precede source"):
            self.run_checks(stages=[self.stages[1], self.stages[0]])
        command.assert_not_called()

    @patch("check_sandbox._command", return_value=completed())
    def test_rejects_workdir_escape_or_non_boolean_network_before_docker(self, command):
        for invalid in ("../outside", "/etc", "/workspace/../etc"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.run_checks(stages=[{"name": "test", "argv": ["true"], "cwd": invalid}])
        with self.assertRaises(ValueError):
            self.run_checks(stages=[{"name": "test", "argv": ["true"], "network": "false"}])
        command.assert_not_called()

    @patch("check_sandbox._command", return_value=completed())
    def test_rejects_image_option_injection(self, command):
        with self.assertRaises(ValueError):
            self.run_checks(image="--privileged")
        command.assert_not_called()

    def test_output_is_drained_with_bounded_retained_tail(self):
        result = _command([sys.executable, "-c", "import sys; sys.stdout.write('x' * 2000000 + 'END')"],
                          timeout=5, output_limit=1234)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(result["output"]), 1234)
        self.assertTrue(result["output"].endswith("END"))
        self.assertTrue(result["output_truncated"])

    def test_command_deadline_kills_process(self):
        result = _command([sys.executable, "-c", "import time; time.sleep(30)"],
                          timeout=0.1, output_limit=100)
        self.assertTrue(result["timed_out"])
        self.assertLess(result["duration_seconds"], 2)


if __name__ == "__main__":
    unittest.main()
