import copy
from datetime import datetime, timezone
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import reviewer as r


PR = {"state": "open", "draft": False, "user": {"login": "author"},
      "requested_reviewers": [{"login": "reviewer"}],
      "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
CLEAN = {"complete": True, "summary": "Fixes retry handling.",
         "limitations": "Static review; tests were not run.", "findings": []}


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]})
        config.start()
        self.addCleanup(config.stop)

    def test_review_model_switches_at_eleven_and_seventeen_in_dar_time(self):
        config = {"model": "gpt-6.1-sol", "reasoning": "xhigh",
                  "daytime_review": {"model": "gpt-6-luna", "reasoning": "max"}}
        cases = [(0, 0, "gpt-6.1-sol", "xhigh"), (10, 59, "gpt-6.1-sol", "xhigh"),
                 (11, 0, "gpt-6-luna", "max"), (16, 59, "gpt-6-luna", "max"),
                 (17, 0, "gpt-6.1-sol", "xhigh"), (23, 59, "gpt-6.1-sol", "xhigh")]
        with patch.object(r, "CONFIG", config):
            for hour, minute, model, effort in cases:
                with self.subTest(hour=hour, minute=minute):
                    now = datetime(2026, 10, 30, hour, minute, tzinfo=r.REVIEW_TIMEZONE)
                    self.assertEqual(r.review_model(now), (model, effort))
                    self.assertEqual(r.review_model(now.astimezone(timezone.utc)), (model, effort))

    def test_review_model_preserves_defaults_when_no_daytime_profile_is_configured(self):
        config = {"model": "existing-model", "reasoning": "high"}
        with patch.object(r, "CONFIG", config):
            self.assertEqual(r.review_model(datetime(2026, 10, 30, 12, tzinfo=r.REVIEW_TIMEZONE)),
                             ("existing-model", "high"))

    def test_poll_schedule_boundaries_in_dar_es_salaam(self):
        cases = [(0, 5, 0, 0, 0, 10), (10, 59, 10, 50, 11, 0),
                 (11, 0, 11, 0, 11, 30), (11, 29, 11, 0, 11, 30),
                 (11, 30, 11, 30, 12, 0), (13, 59, 13, 30, 14, 0),
                 (14, 0, 14, 0, 14, 30), (16, 59, 16, 30, 17, 0),
                 (17, 0, 17, 0, 17, 10), (23, 59, 23, 50, 0, 0)]
        for hour, minute, start_hour, start_minute, next_hour, next_minute in cases:
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 10, 23, hour, minute, tzinfo=r.REVIEW_TIMEZONE)
                start, end = r.poll_window(now)
                self.assertEqual((start.hour, start.minute), (start_hour, start_minute))
                self.assertEqual((end.hour, end.minute), (next_hour, next_minute))
                self.assertLessEqual(start, now)
                self.assertGreater(end, now)

    def test_schedule_uses_dar_time_even_with_utc_input(self):
        start, end = r.poll_window(datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc))
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (11, 0, 11, 30))
        self.assertEqual(start.tzinfo.key, "Africa/Dar_es_Salaam")

    def test_daytime_has_half_hour_windows_and_night_has_ten_minute_windows(self):
        daytime, nighttime = set(), set()
        for hour in range(24):
            for minute in range(60):
                start, _ = r.poll_window(datetime(2026, 10, 23, hour, minute,
                                                tzinfo=r.REVIEW_TIMEZONE))
                (daytime if 11 <= hour < 17 else nighttime).add(start)
        self.assertEqual(len(daytime), 12)
        self.assertEqual(len(nighttime), 18 * 6)

    def test_poll_window_is_claimed_once_across_restarts(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(r, "ROOT", Path(folder)):
            before = datetime(2026, 10, 23, 11, 2, tzinfo=r.REVIEW_TIMEZONE)
            later = datetime(2026, 10, 23, 11, 29, tzinfo=r.REVIEW_TIMEZONE)
            next_window = datetime(2026, 10, 23, 11, 30, tzinfo=r.REVIEW_TIMEZONE)
            with patch.object(r, "poll_window", return_value=r.poll_window(before)):
                self.assertTrue(r.claim_poll_window())
            with patch.object(r, "poll_window", return_value=r.poll_window(later)):
                self.assertFalse(r.claim_poll_window())
            with patch.object(r, "poll_window", return_value=r.poll_window(next_window)):
                self.assertTrue(r.claim_poll_window())

    def test_fast_polling_resumes_at_17_even_after_late_daytime_poll(self):
        before = datetime(2026, 10, 23, 16, 59, tzinfo=r.REVIEW_TIMEZONE)
        after = datetime(2026, 10, 23, 17, 0, tzinfo=r.REVIEW_TIMEZONE)
        self.assertNotEqual(r.poll_window(before)[0], r.poll_window(after)[0])

    def test_reviewer_and_autofixer_claim_independent_windows_with_their_own_logs(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(r, 'ROOT', Path(folder)), \
                patch.object(r.LOG, 'info') as reviewer_log:
            autofixer_log = Mock()
            autofixer_schedule = Path(folder) / 'autofix-poll-schedule.json'
            now = datetime(2026, 10, 30, 17, 0, tzinfo=r.REVIEW_TIMEZONE)
            with patch.object(r, 'poll_window', return_value=r.poll_window(now)):
                self.assertTrue(r.claim_poll_window())
                self.assertTrue(r.claim_poll_window(path=autofixer_schedule, log=autofixer_log))
                self.assertFalse(r.claim_poll_window())
                self.assertFalse(r.claim_poll_window(path=autofixer_schedule, log=autofixer_log))
            self.assertEqual(reviewer_log.call_count, 2)
            self.assertEqual(autofixer_log.info.call_count, 2)

    def test_already_polled_window_skips_all_github_and_model_calls(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(r, "ROOT", Path(folder)), \
                patch("sys.argv", ["reviewer.py"]), \
                patch.object(r, "RotatingFileHandler"), patch.object(r.LOG, "addHandler"), \
                patch.object(r, "claim_poll_window", return_value=False), \
                patch.object(r, "api") as api, patch.object(r, "evaluate") as evaluate:
            (Path(folder) / "config.json").write_text(json.dumps(r.CONFIG))
            r.main()
            api.assert_not_called()
            evaluate.assert_not_called()

    def test_scope_is_exact_and_fails_closed(self):
        with patch.object(r, "CONFIG", {"repositories": ["iPFSoftwares/notify-backend",
                                                          "iPFSoftwares/notify-voice-service"]}):
            self.assertTrue(r.repository_allowed("ipfsoftwares/NOTIFY-BACKEND"))
            self.assertTrue(r.repository_allowed("iPFSoftwares/notify-voice-service"))
            for repo in ("iPFSoftwares/documents-parser", "iPFSoftwares/notify-africa-docs",
                         "other/notify-backend", "iPFSoftwares/notify-backend-extra"):
                self.assertFalse(r.repository_allowed(repo))
        with patch.object(r, "CONFIG", {}):
            self.assertFalse(r.repository_allowed("iPFSoftwares/notify-backend"))

    def test_out_of_scope_pr_cannot_run_even_when_watched(self):
        for watched in (False, True):
            state = {"iPFSoftwares/documents-parser#20": {"watch": watched}}
            with patch.object(r, "api") as api, patch.object(r, "evaluate") as evaluate:
                r.review_one("iPFSoftwares/documents-parser", 20, state)
                api.assert_not_called()
                evaluate.assert_not_called()

    def test_scope_revoked_during_review_prevents_posting(self):
        def evaluate(*args):
            r.CONFIG["repositories"] = []
            return CLEAN
        with patch.object(r, "api", side_effect=[PR, PR]) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", side_effect=evaluate), patch.object(r, "save_state"):
            r.review_one("owner/repo", 1, {})
            self.assertEqual(api.call_count, 2)
            self.assertTrue(all(not call.kwargs for call in api.call_args_list))

    def test_clean_review_approves_pinned_commit_with_lgtm(self):
        payload = r.review_body(CLEAN, "a" * 40)
        self.assertEqual(payload["event"], "APPROVE")
        self.assertTrue(payload["body"].startswith("LGTM"))
        self.assertIn("\n\n**PR Review Agent**. Reviewed commit ", payload["body"])
        self.assertNotIn("Automated review using Code Assurance and Ponytail.", payload["body"])
        self.assertEqual(payload["commit_id"], "a" * 40)

    def test_any_recommended_fix_requests_changes(self):
        result = copy.deepcopy(CLEAN)
        result["findings"] = [{"severity": "Should fix", "file": "src/auth.py", "line": 5,
                               "problem": "A tenant can read another tenant's row.",
                               "fix": "Scope the lookup by tenant ID.",
                               "impact": "Private records are exposed."}]
        payload = r.review_body(result, "a" * 40)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertTrue(payload["body"].startswith("**What this change does:**"))
        self.assertIn("\n\n**PR Review Agent**. Reviewed commit ", payload["body"])
        result["complete"] = False
        partial = r.review_body(result, "a" * 40)
        self.assertEqual(partial["event"], "REQUEST_CHANGES")
        self.assertIn("Partial review", partial["body"])

    def test_incomplete_or_malformed_review_cannot_approve(self):
        self.assertIsNone(r.review_body({**CLEAN, "complete": False}, "a" * 40))
        for invalid in ({}, {**CLEAN, "complete": "true"}, {**CLEAN, "findings": None}):
            with self.assertRaises(ValueError):
                r.review_body(invalid, "a" * 40)

    def test_skip_self_draft_closed_unassigned(self):
        self.assertTrue(r.eligible(PR, "REVIEWER"))
        self.assertFalse(r.eligible(PR, "author", True))
        self.assertFalse(r.eligible({**PR, "draft": True}, "reviewer", True))
        self.assertFalse(r.eligible({**PR, "state": "closed"}, "reviewer", True))
        self.assertFalse(r.eligible({**PR, "requested_reviewers": []}, "reviewer"))
        self.assertTrue(r.eligible({**PR, "requested_reviewers": []}, "reviewer", True))

    def test_all_drafts_skip_evaluation_even_when_requested_or_watched(self):
        for round_policy in (False, True):
            for watched in (False, True):
                for dry_run in (False, True):
                    with self.subTest(round_policy=round_policy, watched=watched, dry_run=dry_run):
                        draft = {**PR, "draft": True, "body": "@reviewer REQUEST REVIEW"}
                        state = {"owner/repo#1": {"watch": watched, "last_attempt": 42}}
                        before = copy.deepcopy(state)
                        with patch.dict(r.CONFIG, draft_at_round_six=round_policy), \
                                patch.object(r, "api", return_value=draft) as api, \
                                patch.object(r, "paginated") as history, \
                                patch.object(r, "evaluate") as evaluate, \
                                patch.object(r, "save_state") as save:
                            r.review_one("owner/repo", 1, state, dry_run=dry_run)
                        self.assertEqual(state, before)
                        api.assert_called_once_with("repos/owner/repo/pulls/1")
                        history.assert_not_called()
                        evaluate.assert_not_called()
                        save.assert_not_called()

    def test_unconfirmed_pending_review_is_not_retried_while_draft(self):
        draft = {**PR, "draft": True}
        state = {"owner/repo#1": {"watch": True, "round_six": {
            "pending": True, "round": 6, "outcome": "REQUEST_CHANGES",
            "reviewed_revision": [PR["head"]["sha"], PR["base"]["sha"]]}}}
        with patch.dict(r.CONFIG, draft_at_round_six=True), \
                patch.object(r, "api", return_value=draft) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate") as evaluate, patch.object(r, "save_state") as save:
            r.review_one("owner/repo", 1, state)
        evaluate.assert_not_called()
        save.assert_not_called()
        self.assertTrue(all(not call.kwargs for call in api.call_args_list))

    def test_remote_review_dedupes_after_uncertain_post(self):
        reviews = [{"user": {"login": "REVIEWER"}, "commit_id": "a" * 40, "state": "APPROVED"}]
        self.assertTrue(r.already_reviewed(reviews, "reviewer", "a" * 40))
        self.assertFalse(r.already_reviewed(reviews, "reviewer", "c" * 40))
        self.assertFalse(r.already_reviewed(reviews, "someone-else", "a" * 40))
        reviews[0]["state"] = "DISMISSED"
        self.assertTrue(r.already_reviewed(reviews, "reviewer", "a" * 40))

    def test_new_head_or_base_is_stale(self):
        self.assertTrue(r.same_revision(PR, PR))
        for side in ("head", "base"):
            changed = copy.deepcopy(PR)
            changed[side]["sha"] = "c" * 40
            self.assertFalse(r.same_revision(PR, changed))

    def test_no_post_if_changed_closed_or_request_removed_during_review(self):
        changes = [{**PR, "head": {"sha": "c" * 40}},
                   {**PR, "base": {"sha": "c" * 40}},
                   {**PR, "draft": True},
                   {**PR, "state": "closed"}, {**PR, "requested_reviewers": []}]
        for fresh in changes:
            with self.subTest(fresh=fresh), patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                    patch.object(r, "api", side_effect=[PR, fresh]) as api, \
                    patch.object(r, "paginated", return_value=[]), \
                    patch.object(r, "evaluate", return_value=CLEAN), patch.object(r, "save_state"):
                r.review_one("owner/repo", 1, {})
                self.assertEqual(api.call_count, 2)
                self.assertTrue(all(not call.kwargs for call in api.call_args_list))

    def test_post_is_pinned_and_watch_survives_failed_response(self):
        state = {}
        with patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(r, "api", side_effect=[PR, PR, {"login": "reviewer"}, RuntimeError("lost response")]) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value=CLEAN), patch.object(r, "save_state"):
            with self.assertRaises(RuntimeError):
                r.review_one("owner/repo", 1, state)
            self.assertTrue(state["owner/repo#1"]["watch"])
            self.assertEqual(api.call_args.kwargs["payload"]["commit_id"], "a" * 40)

    def test_switched_github_account_cannot_post(self):
        with patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(r, "api", side_effect=[PR, PR, {"login": "other-account"}]) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value=CLEAN), patch.object(r, "save_state"):
            with self.assertRaisesRegex(RuntimeError, "account changed"):
                r.review_one("owner/repo", 1, {})
            self.assertTrue(all(not call.kwargs for call in api.call_args_list))

    def test_incomplete_review_never_reaches_post(self):
        with patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(r, "api", return_value=PR) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value={**CLEAN, "complete": False}), \
                patch.object(r, "save_state"):
            r.review_one("owner/repo", 1, {})
            self.assertEqual(api.call_count, 1)

    def test_dry_run_never_posts_or_tracks(self):
        state = {}
        with patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(r, "api", side_effect=[PR, PR]) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value=CLEAN), patch.object(r, "save_state") as save:
            r.review_one("owner/repo", 1, state, dry_run=True)
            self.assertEqual(state, {})
            save.assert_not_called()
            self.assertEqual(api.call_count, 2)

    def test_closed_dry_run_does_not_change_tracking(self):
        state = {"owner/repo#1": {"watch": True}}
        with patch.object(r, "api", return_value={**PR, "state": "closed"}), \
                patch.object(r, "save_state") as save:
            r.review_one("owner/repo", 1, state, dry_run=True)
            self.assertIn("owner/repo#1", state)
            save.assert_not_called()

    def test_safe_archive_extraction_rejects_traversal_and_omits_links(self):
        def archive_bytes(name, link=False):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as archive:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                    archive.addfile(member)
                else:
                    member.size = 2
                    archive.addfile(member, io.BytesIO(b"ok"))
            return data.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                r.extract_snapshot(archive_bytes("repo/../../escape"), root)
            self.assertEqual(r.extract_snapshot(archive_bytes("repo/secret", True), root), ["secret"])
            self.assertFalse((root / "secret").exists())
            r.extract_snapshot(archive_bytes("repo/src/main.py"), root)
            self.assertEqual((root / "src/main.py").read_text(), "ok")

    def test_submodule_sources_use_only_github_and_reject_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".gitmodules").write_text('[submodule "shared"]\npath = libs/shared\nurl = git@github.com:org/shared.git\n')
            self.assertEqual(r.submodule_sources(root), {"libs/shared": "org/shared"})
            (root / ".gitmodules").write_text('[submodule "bad"]\npath = ../outside\nurl = https://github.com/org/shared.git\n')
            with self.assertRaises(ValueError):
                r.submodule_sources(root)

    def test_unchanged_deferred_commit_does_not_invoke_codex(self):
        state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR)}}
        with patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(r, "api", return_value=PR), \
                patch.object(r, "paginated", return_value=[]), patch.object(r, "evaluate") as evaluate:
            r.review_one("owner/repo", 1, state)
            evaluate.assert_not_called()

    def test_new_primary_head_or_base_retries_without_refreshing_old_companions(self):
        companion = {"repository": "owner/shared", "number": 2, "sha": "c" * 40}
        for side in ("head", "base"):
            updated = copy.deepcopy(PR)
            updated[side]["sha"] = "d" * 40
            state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR, [companion])}}
            with self.subTest(side=side), patch.object(r, "api", return_value=updated) as api, \
                    patch.object(r, "paginated", return_value=[]), \
                    patch.object(r, "evaluate", return_value={**CLEAN, "complete": False}) as evaluate, \
                    patch.object(r, "save_state"):
                r.review_one("owner/repo", 1, state)
                evaluate.assert_called_once()
                api.assert_called_once_with("repos/owner/repo/pulls/1")

    def test_old_deferral_retries_once_with_new_policy(self):
        for saved in (["a" * 40, "b" * 40], ["a" * 40, "b" * 40, "old-policy"]):
            state = {"owner/repo#1": {"deferred_revision": saved}}
            with self.subTest(saved=saved), patch.object(r, "api", return_value=PR) as api, \
                    patch.object(r, "paginated", return_value=[]), \
                    patch.object(r, "evaluate", return_value={**CLEAN, "complete": False}) as evaluate, \
                    patch.object(r, "save_state"):
                r.review_one("owner/repo", 1, state)
                r.review_one("owner/repo", 1, state)
                self.assertEqual(evaluate.call_count, 1)
                self.assertEqual(state["owner/repo#1"]["deferred_revision"], r.deferred_revision(PR))
                self.assertEqual(api.call_count, 2)

    def test_context_source_allowlists_change_retry_key(self):
        original = r.deferred_revision(PR)
        with patch.dict(r.CONFIG, {"context_repositories": ["owner/shared"]}):
            self.assertNotEqual(original, r.deferred_revision(PR))
        with patch.dict(r.CONFIG, {"repositories": ["owner/repo", "owner/another"]}):
            self.assertNotEqual(original, r.deferred_revision(PR))
        with patch.object(r, "CONTEXT_POLICY_VERSION", r.CONTEXT_POLICY_VERSION + 1):
            self.assertNotEqual(original, r.deferred_revision(PR))

    def test_remote_review_still_prevents_post_after_policy_change(self):
        state = {"owner/repo#1": {"deferred_revision": ["a" * 40, "b" * 40, "old"]}}
        reviews = [{"user": {"login": "reviewer"}, "commit_id": "a" * 40, "state": "APPROVED"}]
        with patch.object(r, "api", return_value=PR), \
                patch.object(r, "paginated", return_value=reviews), patch.object(r, "evaluate") as evaluate:
            r.review_one("owner/repo", 1, state)
            evaluate.assert_not_called()

    def test_comment_changes_wake_deferred_review_once(self):
        original = {"id": 1, "body": "Bridge evidence", "updated_at": "2026-10-29T00:00:00Z"}
        changes = [("added", [], [original]),
                   ("edited", [original], [{**original, "body": "New evidence"}]),
                   ("deleted", [original], [])]
        for kind in ("conversation_comments", "review_comments"):
            for label, before, after in changes:
                with self.subTest(kind=kind, change=label):
                    state = {"owner/repo#1": {"deferred_revision": r.deferred_revision({**PR, kind: before})}}
                    comment_endpoint = ("repos/owner/repo/issues/1/comments?per_page=100"
                                        if kind == "conversation_comments" else
                                        "repos/owner/repo/pulls/1/comments?per_page=100")
                    with patch.object(r, "api", return_value=PR), \
                            patch.object(r, "paginated", side_effect=lambda endpoint:
                                         after if endpoint == comment_endpoint else []), \
                            patch.object(r, "evaluate", return_value={**CLEAN, "complete": False}) as evaluate, \
                            patch.object(r, "save_state"):
                        r.review_one("owner/repo", 1, state)
                        r.review_one("owner/repo", 1, state)
                        evaluate.assert_called_once()
                        self.assertEqual(evaluate.call_args.args[2][kind], after)

    def test_comment_fetch_failure_preserves_deferred_state(self):
        state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR)}}
        before = copy.deepcopy(state)
        with patch.object(r, "api", return_value=PR), \
                patch.object(r, "paginated", side_effect=[[], RuntimeError("network unavailable")]), \
                patch.object(r, "evaluate") as evaluate, patch.object(r, "save_state") as save:
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                r.review_one("owner/repo", 1, state)
            self.assertEqual(state, before)
            evaluate.assert_not_called()
            save.assert_not_called()

    def test_context_contains_general_and_inline_comments(self):
        pr = {**PR, "title": "Fix", "body": "", "changed_files": 0}
        general = [{"id": 1, "body": "General evidence"}]
        inline = [{"id": 2, "body": "Inline evidence"}]
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            member = tarfile.TarInfo("repo/README.md")
            member.size = 2
            archive.addfile(member, io.BytesIO(b"ok"))
        def paginated(endpoint):
            if "/issues/" in endpoint: return general
            if "/comments?" in endpoint: return inline
            return []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(r, "gh", side_effect=lambda *args:
                             data.getvalue() if "/tarball/" in args[-1] else b"diff"), \
                patch.object(r, "api", return_value=pr), \
                patch.object(r, "paginated", side_effect=paginated), \
                patch.object(r, "expand_referenced_submodules", return_value={}), \
                patch.object(r, "collect_companion_context", return_value={}) as collect:
            context = r.prepare_context(Path(folder), "owner/repo", 1, pr, [])
            self.assertEqual(context["conversation_comments"], general)
            self.assertEqual(context["review_comments"], inline)
            self.assertEqual(collect.call_args.kwargs["comments"], general + inline)

    def test_adding_companion_citation_to_description_retries_once(self):
        state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR)}}
        updated = {**PR, "body": "Engine companion commit `ab12cd3`."}
        with patch.object(r, "api", return_value=updated), \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value={**CLEAN, "complete": False}) as evaluate, \
                patch.object(r, "save_state"):
            r.review_one("owner/repo", 1, state)
            r.review_one("owner/repo", 1, state)
            self.assertEqual(evaluate.call_count, 1)

    def test_changed_companion_head_retries_once_without_changing_primary_pr(self):
        old = {"repository": "owner/shared", "number": 2, "sha": "c" * 40}
        new = {**old, "sha": "d" * 40}
        state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR, [old])}}
        result = {**CLEAN, "complete": False, "_companion_prs": [new]}
        def api(endpoint):
            return PR if endpoint.endswith("/pulls/1") else {"head": {"sha": new["sha"]}}
        with patch.object(r, "api", side_effect=api), \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value=result) as evaluate, patch.object(r, "save_state"):
            r.review_one("owner/repo", 1, state)
            r.review_one("owner/repo", 1, state)
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(state["owner/repo#1"]["deferred_revision"], r.deferred_revision(PR, [new]))

    def test_unchanged_companion_head_skips_model_and_state_writes(self):
        companion = {"repository": "owner/shared", "number": 2, "sha": "c" * 40}
        state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR, [companion])}}
        with patch.object(r, "api", side_effect=[PR, {"head": {"sha": companion["sha"]}}]), \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate") as evaluate, patch.object(r, "save_state") as save:
            r.review_one("owner/repo", 1, state)
            evaluate.assert_not_called()
            save.assert_not_called()

    def test_companion_api_failure_preserves_deferral_without_model_call(self):
        companion = {"repository": "owner/shared", "number": 2, "sha": "c" * 40}
        state = {"owner/repo#1": {"deferred_revision": r.deferred_revision(PR, [companion])}}
        before = copy.deepcopy(state)
        with patch.object(r, "api", side_effect=[PR, RuntimeError("network unavailable")]), \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate") as evaluate, patch.object(r, "save_state") as save:
            with self.assertRaisesRegex(RuntimeError, "network unavailable"):
                r.review_one("owner/repo", 1, state)
            self.assertEqual(state, before)
            evaluate.assert_not_called()
            save.assert_not_called()

    def test_revoked_context_source_is_not_refreshed_from_old_state(self):
        companion = {"repository": "owner/shared", "number": 2, "sha": "c" * 40}
        with patch.dict(r.CONFIG, {"context_repositories": ["owner/shared"]}):
            saved = r.deferred_revision(PR, [companion])
        state = {"owner/repo#1": {"deferred_revision": saved}}
        with patch.object(r, "api", return_value=PR) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value={**CLEAN, "complete": False}) as evaluate, \
                patch.object(r, "save_state"):
            r.review_one("owner/repo", 1, state)
            evaluate.assert_called_once()
            api.assert_called_once_with("repos/owner/repo/pulls/1")

    def test_snapshot_failure_sets_retry_deadline(self):
        state = {}
        result = {**CLEAN, "complete": False, "_companion_unavailable": True}
        with patch.object(r, "api", return_value=PR), patch.object(r, "time") as clock, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value=result) as evaluate, patch.object(r, "save_state"):
            clock.time.return_value = 1000
            r.review_one("owner/repo", 1, state)
            self.assertEqual(state["owner/repo#1"]["deferred_retry_at"], 4600)
            r.review_one("owner/repo", 1, state)
            self.assertEqual(evaluate.call_count, 1)
            clock.time.return_value = 4601
            r.review_one("owner/repo", 1, state)
            self.assertEqual(evaluate.call_count, 2)

    def test_companion_changed_during_review_prevents_posting(self):
        companion = {"repository": "owner/shared", "number": 2, "sha": "c" * 40}
        with patch.object(r, "api", side_effect=[PR, PR, {"head": {"sha": "d" * 40}}]) as api, \
                patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "evaluate", return_value={**CLEAN, "_companion_prs": [companion]}), \
                patch.object(r, "save_state"):
            r.review_one("owner/repo", 1, {})
            self.assertEqual(api.call_count, 3)
            self.assertTrue(all(not call.kwargs for call in api.call_args_list))

    def test_standalone_service_loads_linked_pr_from_configured_review_repositories(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            text = b"Pairs with owner/shared#2."
            member = tarfile.TarInfo("repo/docs/task.md")
            member.size = len(text)
            archive.addfile(member, io.BytesIO(text))
        pr = {**PR, "title": "Fix", "body": "", "changed_files": 1}
        def gh(*args):
            return data.getvalue() if "/tarball/" in args[-1] else b"diff"
        def api(endpoint):
            return {"head": {"sha": "c" * 40}} if endpoint.endswith("/pulls/2") else pr
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(r.CONFIG, {"repositories": ["owner/repo", "owner/shared"]}), \
                patch.object(r, "gh", side_effect=gh) as github, patch.object(r, "api", side_effect=api), \
                patch.object(r, "paginated", side_effect=lambda endpoint:
                             [{"filename": "docs/task.md"}] if "/files?" in endpoint else []), \
                patch.object(r, "expand_referenced_submodules", return_value={}):
            context = r.prepare_context(Path(folder), "owner/repo", 1, pr, [])
            companion = context["companion_evidence"]
            self.assertEqual(companion["pull_requests"],
                             [{"repository": "owner/shared", "number": 2, "sha": "c" * 40}])
            self.assertEqual(companion["unavailable"], [])
            github.assert_any_call("api", "--hostname", "github.com", "repos/owner/shared/tarball/" + "c" * 40)
            self.assertTrue((Path(folder) / companion["snapshots"][0]["path"] / "docs/task.md").is_file())


if __name__ == "__main__":
    unittest.main()
