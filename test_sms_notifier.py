import copy
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import reviewer as reviewer
import sms_notifier as sms
from test_reviewer import CLEAN, PR


REPO = "iPFSoftwares/notify-voice-service"
SETTINGS = {"enabled": True, "enabled_at": datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp(),
            "recipient": "+255694192317", "sender_id": "4773e08c-1779-5fe2-9a57-80396bad7969"}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.log = Mock(spec=logging.Logger)
        sender = patch.object(sms, "send_sms", return_value="sms-123")
        self.send = sender.start()
        self.addCleanup(sender.stop)
        writer = patch.object(sms, "generate_sms", side_effect=lambda root, fact, repo, number: fact)
        self.writer = writer.start()
        self.addCleanup(writer.stop)

    def notifier(self):
        return sms.SmsNotifier(self.root, SETTINGS, self.log)

    def observe(self, **kwargs):
        # Each scheduled reviewer poll creates a fresh notifier.
        self.notifier().observe(REPO, 34, **kwargs)

    def test_fourth_deferred_poll_alerts_once_and_survives_restarts(self):
        for _ in range(3):
            self.observe(deferred=True)
        self.send.assert_not_called()
        self.observe(deferred=True)
        self.assertIn("4 consecutive polls", self.send.call_args.args[1])
        self.assertIn("https://github.com/" + REPO + "/pull/34", self.send.call_args.args[1])
        for _ in range(5):
            self.observe(deferred=True)
        self.assertEqual(self.send.call_count, 1)

    def test_resolved_deferral_resets_counter_and_allows_a_new_episode(self):
        for _ in range(4):
            self.observe(deferred=True)
        self.observe(deferred=False)
        for _ in range(3):
            self.observe(deferred=True)
        self.assertEqual(self.send.call_count, 1)
        self.observe(deferred=True)
        self.assertEqual(self.send.call_count, 2)

    def test_round_six_alerts_once_per_pr(self):
        self.observe(round_number=5)
        self.send.assert_not_called()
        self.observe(round_number=6)
        self.observe(round_number=6)
        self.observe(round_number=7)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("round 6", self.send.call_args.args[1])

    def test_approval_alert_is_deduplicated_by_commit(self):
        self.observe(approval_sha="a" * 40)
        self.observe(approval_sha="a" * 40)
        self.assertEqual(self.send.call_count, 1)
        self.observe(approval_sha="b" * 40)
        self.assertEqual(self.send.call_count, 2)

    def test_notify_backend_excludes_only_approvals(self):
        repo = "iPFSoftwares/NOTIFY-BACKEND"
        self.notifier().observe(repo, 123, approval_sha="a" * 40)
        self.send.assert_not_called()
        for _ in range(4):
            self.notifier().observe(repo, 123, deferred=True)
        self.notifier().observe(repo, 123, round_number=6)
        self.assertEqual(self.send.call_count, 2)

    def test_failed_sends_remain_pending_and_retry_once_per_poll(self):
        self.send.side_effect = RuntimeError("offline")
        notifier = self.notifier()
        notifier.observe(REPO, 34, approval_sha="a" * 40)
        notifier.observe(REPO, 34, deferred=False)
        self.assertEqual(self.send.call_count, 1)
        saved = json.loads((self.root / "sms-notifications.json").read_text())
        self.assertIn("approved:" + "a" * 40, saved[REPO + "#34"]["pending"])
        self.send.side_effect = None
        self.observe(deferred=False)
        self.observe(deferred=False)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.writer.call_count, 1)

    def test_ai_failure_keeps_alert_pending_without_a_template_fallback(self):
        self.writer.side_effect = RuntimeError("AI unavailable")
        self.observe(approval_sha="a" * 40)
        self.send.assert_not_called()
        self.writer.side_effect = None
        self.writer.return_value = "Your PR is approved. https://github.com/" + REPO + "/pull/34"
        self.observe(deferred=False)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.writer.call_count, 2)

    def test_invalid_generated_sms_is_never_sent(self):
        for content in ("", "x" * 919, "PR approved \U0001f389"):
            with self.subTest(content_length=len(content)):
                self.writer.side_effect = None
                self.writer.return_value = content
                self.observe(approval_sha=str(len(content)) * 40)
                self.send.assert_not_called()

    def test_cached_ai_wording_is_reused_after_sms_api_failure(self):
        self.writer.side_effect = None
        self.writer.return_value = "notify-voice-service#34: Your PR is approved. https://github.com/" + REPO + "/pull/34"
        self.send.side_effect = RuntimeError("SMS offline")
        self.observe(approval_sha="a" * 40)
        pending = json.loads((self.root / "sms-notifications.json").read_text())[REPO + "#34"]["pending"]
        self.assertEqual(pending["approved:" + "a" * 40]["content"], self.writer.return_value)
        self.send.side_effect = None
        self.observe(deferred=False)
        self.assertEqual(self.writer.call_count, 1)
        self.assertEqual(self.send.call_args.args[1], self.writer.return_value)

    def test_resolved_pr_cancels_an_unsent_deferred_alert(self):
        self.send.side_effect = RuntimeError("offline")
        for _ in range(4):
            self.observe(deferred=True)
        self.send.side_effect = None
        self.observe(deferred=False)
        self.assertEqual(self.send.call_count, 1)

    def test_only_new_agent_approvals_are_recovered(self):
        review = {"state": "APPROVED", "commit_id": "a" * 40,
                  "user": {"login": "Johnndelembi"}, "body": reviewer.MARKER,
                  "submitted_at": "2026-10-30T07:00:00Z"}
        for changes in ({"submitted_at": "2026-10-29T07:00:00Z"},
                        {"body": "human approval"}, {"user": {"login": "someone-else"}},
                        {"commit_id": "b" * 40}, {"state": "CHANGES_REQUESTED"}):
            self.notifier().observe_reviews(REPO, 34, [{**review, **changes}],
                                           "Johnndelembi", reviewer.MARKER, "a" * 40)
        self.send.assert_not_called()
        for _ in range(2):
            self.notifier().observe_reviews(REPO, 34, [review],
                                           "Johnndelembi", reviewer.MARKER, "a" * 40)
        self.assertEqual(self.send.call_count, 1)

    def test_disabled_notifications_do_not_write_or_send(self):
        sms.SmsNotifier(self.root, {"enabled": False}, self.log).observe(
            REPO, 34, approval_sha="a" * 40, round_number=6, deferred=True)
        self.send.assert_not_called()
        self.assertFalse((self.root / "sms-notifications.json").exists())

    def test_corrupt_state_does_not_send_or_get_overwritten(self):
        path = self.root / "sms-notifications.json"
        path.write_text("broken-json")
        self.observe(approval_sha="a" * 40)
        self.send.assert_not_called()
        self.assertEqual(path.read_text(), "broken-json")
        self.log.exception.assert_called_once()

    def test_reviewer_counts_initial_and_unchanged_deferrals(self):
        state = {}
        pr = {**PR, "conversation_comments": [], "review_comments": []}
        with patch.object(reviewer, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(reviewer, "api", return_value=PR), \
                patch.object(reviewer, "paginated", return_value=[]), \
                patch.object(reviewer, "evaluate", return_value={**CLEAN, "complete": False}) as evaluate, \
                patch.object(reviewer, "save_state"):
            for _ in range(4):
                reviewer.review_one("owner/repo", 1, state, notifier=self.notifier())
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(state["owner/repo#1"]["deferred_revision"], reviewer.deferred_revision(pr))
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("4 consecutive polls", self.send.call_args.args[1])

    def test_reviewer_sends_approval_only_after_successful_post(self):
        for fail in (True, False):
            with self.subTest(fail=fail):
                self.send.reset_mock()
                posted = RuntimeError("lost response") if fail else {"html_url": "https://github.com/review/1"}
                with patch.object(reviewer, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                        patch.object(reviewer, "api", side_effect=[PR, PR, {"login": "reviewer"}, posted]), \
                        patch.object(reviewer, "paginated", return_value=[]), \
                        patch.object(reviewer, "evaluate", return_value=copy.deepcopy(CLEAN)), \
                        patch.object(reviewer, "save_state"):
                    if fail:
                        with self.assertRaises(RuntimeError):
                            reviewer.review_one("owner/repo", 1, {}, notifier=self.notifier())
                        self.send.assert_not_called()
                    else:
                        reviewer.review_one("owner/repo", 1, {}, notifier=self.notifier())
                        self.assertEqual(self.send.call_count, 1)

    def test_dry_run_ignores_notifier(self):
        notifier = Mock()
        with patch.object(reviewer, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(reviewer, "api", side_effect=[PR, PR]), \
                patch.object(reviewer, "paginated", return_value=[]), \
                patch.object(reviewer, "evaluate", return_value=CLEAN):
            reviewer.review_one("owner/repo", 1, {}, dry_run=True, notifier=notifier)
        self.assertFalse(notifier.mock_calls)


class TransportTests(unittest.TestCase):
    def test_transport_enforces_limit_before_loading_keys_or_calling_api(self):
        with patch.object(sms, "urlopen") as http:
            for content in ("", " " * 5, "x" * 919, "approved \U0001f389"):
                with self.subTest(content_length=len(content)), self.assertRaises(ValueError):
                    sms.send_sms(SETTINGS, content)
            http.assert_not_called()

    def test_transport_reads_mcp_key_and_uses_working_api_payload(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sms.Path, "home", return_value=Path(folder)):
            config = Path(folder) / ".codex/config.toml"
            config.parent.mkdir()
            config.write_text('[mcp_servers.notify-bulk-sms.env]\nNOTIFY_API_KEY="test-key"\n'
                              'NOTIFY_BULK_SMS_API_BASE_URL="https://api.notify.africa/api/v1"\n')
            response = Mock()
            response.__enter__ = Mock(return_value=io.StringIO('{"data":{"messageId":"abc","status":"SENT"}}'))
            response.__exit__ = Mock(return_value=False)
            with patch.object(sms, "urlopen", return_value=response) as http:
                self.assertEqual(sms.send_sms(SETTINGS, "example alert"), "abc")
            request = http.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.notify.africa/api/v1/api/messages/send")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(json.loads(request.data), {"senderId": SETTINGS["sender_id"],
                             "recipient": SETTINGS["recipient"], "content": "example alert"})
            with patch.object(sms, "urlopen", side_effect=HTTPError(request.full_url, 401, "unauthorized", {}, None)):
                with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                    sms.send_sms(SETTINGS, "example alert")


class AiWriterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "config.json").write_text(json.dumps({"codex": "/test/codex", "model": "gpt-6.1-sol"}))

    def process(self, body):
        def start(args, **kwargs):
            Path(args[args.index("--output-last-message") + 1]).write_text(body)
            return Mock(returncode=0, pid=123, communicate=Mock(return_value=("", "")))
        return patch.object(sms.subprocess, "Popen", side_effect=start)

    def test_generates_actual_wording_and_adds_pr_identifier_and_link(self):
        with self.process("Review has reached round six. Please take a look.") as command:
            content = sms.generate_sms(self.root, "Review reached round 6", REPO, 34)
        self.assertTrue(content.startswith("notify-voice-service#34: Review has reached round six."))
        self.assertTrue(content.endswith("https://github.com/" + REPO + "/pull/34"))
        self.assertLessEqual(len(content), sms.MAX_SMS_CHARS)
        args = command.call_args.args[0]
        self.assertIn("--ignore-user-config", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[args.index("--model") + 1], "gpt-6.1-sol")

    def test_entire_message_including_url_must_fit_the_limit(self):
        prefix = "notify-voice-service#34: "
        suffix = " https://github.com/" + REPO + "/pull/34"
        budget = sms.MAX_SMS_CHARS - len(prefix) - len(suffix)
        with self.process("x" * budget):
            self.assertEqual(len(sms.generate_sms(self.root, "Approved", REPO, 34)), sms.MAX_SMS_CHARS)
        with self.process("x" * (budget + 1)), self.assertRaises(ValueError):
            sms.generate_sms(self.root, "Approved", REPO, 34)

    def test_empty_emoji_and_invented_url_outputs_are_rejected(self):
        for body in ("", "Approved \U0001f389", "Approved https://example.com"):
            with self.subTest(body=body), self.process(body), self.assertRaises(ValueError):
                sms.generate_sms(self.root, "Approved", REPO, 34)

    def test_notification_and_github_secrets_are_not_passed_to_writer(self):
        with patch.dict(os.environ, {"NOTIFY_API_KEY": "secret", "GH_TOKEN": "secret", "OPENAI_API_KEY": "secret"}), \
                self.process("Your PR is approved.") as command:
            sms.generate_sms(self.root, "Approved", REPO, 34)
        environment = command.call_args.kwargs["env"]
        for key in ("NOTIFY_API_KEY", "GH_TOKEN", "OPENAI_API_KEY"):
            self.assertNotIn(key, environment)

    def test_model_failure_is_reported_instead_of_using_a_template(self):
        process = Mock(returncode=1, communicate=Mock(return_value=("", "usage limit reached")))
        with patch.object(sms.subprocess, "Popen", return_value=process), \
                self.assertRaisesRegex(RuntimeError, "usage limit reached"):
            sms.generate_sms(self.root, "Approved", REPO, 34)

    def test_generation_timeout_stops_process_group(self):
        process = Mock(pid=123, communicate=Mock(side_effect=[
            subprocess.TimeoutExpired("codex", 60), ("", "")]))
        with patch.object(sms.subprocess, "Popen", return_value=process), \
                patch.object(sms.os, "killpg") as kill, \
                self.assertRaisesRegex(RuntimeError, "timed out"):
            sms.generate_sms(self.root, "Approved", REPO, 34)
        kill.assert_called_once_with(123, sms.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
