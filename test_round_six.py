import copy
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import reviewer as r
from test_reviewer import CLEAN, PR
from test_review_policy import finding, result, review


class RoundSixTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.pr = {**copy.deepcopy(PR), "node_id": "PR_fixture", "number": 1}
        self.reviews = [review(n) for n in range(1, 6)]
        self.comments = []
        self.state = {}
        self.actions = []
        self.posts = []
        self.login = "reviewer"
        self.lost_draft_response = False
        self.lost_comment_response = False
        self.forbid_draft = False
        self.changed_after_draft = False
        self.changed_account_after_draft = False
        self.lost_review_response = False
        self.notifier = Mock()
        self.notifier.observe.side_effect = lambda *args, **kwargs: self.actions.append("sms") if kwargs.get("round_number") or kwargs.get("approval_sha") else None
        for patcher in (patch.object(r, "ROOT", self.root),
                        patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"],
                                                   "draft_at_round_six": True}),
                        patch.object(r, "api", side_effect=self.api),
                        patch.object(r, "paginated", side_effect=self.paginated)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.result = result([finding("unresolved", prior_finding_id="review-1-finding-1")])
        self.result["_review_policy"] = {"round": 6,
            "findings": [{"id": "review-1-finding-1", "index": 1, "blocking": True}],
            "resolution_counts": {"resolved": 4, "unresolved": 1, "unverified": 0}}
        evaluate = patch.object(r, "evaluate", side_effect=self.assess)
        self.evaluate = evaluate.start()
        self.addCleanup(evaluate.stop)

    def assess(self, *args):
        self.actions.append("evaluate")
        return copy.deepcopy(self.result)

    def api(self, endpoint, *, payload=None):
        if payload is None:
            if endpoint == "user":
                return {"login": self.login}
            self.assertEqual(endpoint, "repos/owner/repo/pulls/1")
            return copy.deepcopy(self.pr)
        self.posts.append((endpoint, payload))
        if endpoint == "graphql":
            self.assertIn("convertPullRequestToDraft", payload["query"])
            self.assertEqual(payload["variables"], {"id": "PR_fixture"})
            if self.forbid_draft:
                return {"errors": [{"message": "Resource not accessible"}]}
            self.pr["draft"] = True
            self.actions.append("draft")
            if self.changed_after_draft:
                self.pr["head"]["sha"] = "c" * 40
            if self.changed_account_after_draft:
                self.login = "other-account"
            if self.lost_draft_response:
                self.lost_draft_response = False
                raise RuntimeError("lost draft response")
            return {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}}
        if endpoint == "repos/owner/repo/issues/1/comments":
            self.actions.append("comment")
            comment = {"id": 1234, "body": payload["body"], "user": {"login": "reviewer"}}
            self.comments.append(comment)
            if self.lost_comment_response:
                self.lost_comment_response = False
                raise RuntimeError("lost comment response")
            return copy.deepcopy(comment)
        self.assertEqual(endpoint, "repos/owner/repo/pulls/1/reviews")
        self.actions.append(payload["event"])
        posted = {"id": 9999, "html_url": "https://github.com/owner/repo/pull/1#pullrequestreview-9999",
                  "body": payload["body"], "commit_id": payload["commit_id"], "user": {"login": "reviewer"},
                  "state": {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}[payload["event"]]}
        self.reviews.append(posted)
        if self.lost_review_response:
            self.lost_review_response = False
            raise RuntimeError("lost review response")
        return copy.deepcopy(posted)

    def paginated(self, endpoint):
        if endpoint.endswith("/reviews?per_page=100"):
            return copy.deepcopy(self.reviews)
        if endpoint == "repos/owner/repo/issues/1/comments?per_page=100":
            return copy.deepcopy(self.comments)
        self.assertEqual(endpoint, "repos/owner/repo/pulls/1/comments?per_page=100")
        return []

    def run_review(self, *, dry_run=False):
        r.review_one("owner/repo", 1, self.state, dry_run=dry_run, notifier=self.notifier)

    def test_sixth_round_reviews_and_requests_changes_before_draft_notice_and_sms(self):
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "draft", "comment", "sms"])
        self.assertTrue(self.pr["draft"])
        self.evaluate.assert_called_once()
        self.assertEqual([endpoint for endpoint, _ in self.posts],
                         ["repos/owner/repo/pulls/1/reviews", "graphql", "repos/owner/repo/issues/1/comments"])
        self.assertNotIn(r.MARKER, self.comments[0]["body"])
        self.assertIn(r.ROUND_SIX_MARKER, self.comments[0]["body"])
        self.notifier.observe.assert_any_call("owner/repo", 1, deferred=False, round_number=6)

    def test_comment_tags_author_and_cites_five_actual_commits_and_reviews(self):
        self.run_review()
        body = self.comments[0]["body"]
        self.assertIn("@author", body)
        self.assertIn("5 distinct commits received change requests", body)
        self.assertIn("latest commit was reviewed", body)
        self.assertIn("Tenant access is not checked.", body)
        self.assertIn("Ready for review", body)
        self.assertIn("ready to merge", body)
        for item in self.reviews[:5]:
            self.assertIn("https://github.com/owner/repo/commit/" + item["commit_id"], body)
            self.assertIn("#pullrequestreview-" + str(item["id"]), body)

    def test_notice_can_extract_issue_evidence_from_legacy_plain_labels(self):
        for item in self.reviews:
            item["body"] = item["body"].replace("**Problem:**", "Problem:")
        self.run_review()
        self.assertIn("Tenant access is not checked.", self.comments[0]["body"])

    def test_table_cells_escape_pipe_characters_without_adding_columns(self):
        self.reviews[-1]["body"] = self.reviews[-1]["body"].replace(
            "auth.py", "auth|guard.py").replace(
            "Tenant access is not checked.", "The ready|pending state is not checked.")
        self.run_review()
        body = self.comments[0]["body"]
        self.assertIn("ready&#124;pending", body)
        self.assertIn(r"auth\|guard.py:1", body)
        rows = [line for line in body.splitlines() if line.startswith("|")]
        self.assertTrue(rows)
        self.assertTrue(all(len(re.split(r"(?<!\\)\|", row)) == 5 for row in rows))

    def test_existing_draft_is_skipped_even_at_round_six(self):
        self.pr["draft"] = True
        with patch.object(r, "paginated") as history:
            self.run_review()
            history.assert_not_called()
        self.evaluate.assert_not_called()
        self.assertFalse(self.actions)
        self.assertFalse(self.posts)
        self.assertTrue(self.pr["draft"])

    def test_clean_sixth_round_approves_without_drafting_or_author_notice(self):
        self.result = copy.deepcopy(CLEAN)
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "APPROVE", "sms", "sms"])
        self.assertFalse(self.pr["draft"])
        self.assertFalse(self.comments)
        record = self.state["owner/repo#1"]["round_six"]
        self.assertEqual(record["outcome"], "APPROVE")
        self.assertTrue(record["completed"])
        self.assertFalse(record["pending"])
        self.assertEqual(self.posts[0][1]["commit_id"], self.pr["head"]["sha"])
        self.notifier.observe.assert_any_call("owner/repo", 1, deferred=False,
                                             approval_sha=self.pr["head"]["sha"])

    def test_clean_result_cannot_approve_an_existing_draft(self):
        self.pr["draft"] = True
        self.result = copy.deepcopy(CLEAN)
        self.run_review()
        self.assertTrue(self.pr["draft"])
        self.assertFalse(self.comments)
        self.assertFalse(self.posts)
        self.evaluate.assert_not_called()

    def test_late_discovery_stays_comment_instead_of_inventing_change_request(self):
        self.result["_review_policy"]["findings"][0]["blocking"] = False
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "COMMENT", "draft", "comment", "sms"])
        self.assertIn("separate **COMMENT** review", self.comments[0]["body"])

    def test_incomplete_sixth_round_is_assessed_and_notice_explains_deferral(self):
        self.result = {**CLEAN, "complete": False, "limitations": "Provider evidence is unavailable."}
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "draft", "comment", "sms"])
        self.assertIn("review is **deferred**", self.comments[0]["body"])
        self.assertIn("no new code defect is asserted", self.comments[0]["body"])
        self.assertFalse(any(endpoint.endswith("/reviews") for endpoint, _ in self.posts))
        self.assertEqual(self.state["owner/repo#1"]["round_six"]["outcome"], "DEFERRED")

    def test_model_error_cannot_trigger_round_six_notice_or_sms(self):
        self.evaluate.side_effect = RuntimeError("model unavailable")
        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            self.run_review()
        self.assertFalse(self.posts)
        self.assertFalse(self.comments)
        self.assertNotIn("sms", self.actions)
        self.assertFalse(self.pr["draft"])

    def test_resolved_findings_are_reported_before_the_round_six_deferral_notice(self):
        history = r.review_history(self.reviews, 'reviewer', r.MARKER)
        self.result = {**CLEAN, 'complete': False, 'prior_finding_statuses': [
            {'id': item['id'], 'status': 'resolved', 'evidence': 'Current source verifies the fix.'}
            for item in history['prior_findings']]}
        self.run_review()
        self.assertEqual(self.actions, ['evaluate', 'comment', 'draft', 'comment', 'sms'])
        self.assertIn(r.RESOLUTION_MARKER, self.comments[0]['body'])
        self.assertIn('review is **deferred**', self.comments[1]['body'])
        self.assertFalse(any(endpoint.endswith('/reviews') for endpoint, _ in self.posts))
        self.assertEqual(self.state['owner/repo#1']['round_six']['outcome'], 'DEFERRED')

    def test_legacy_draft_stays_paused_despite_cached_deferral_and_new_commits(self):
        self.pr["draft"] = True
        self.comments.append({"id": 1234, "body": r.ROUND_SIX_MARKER, "user": {"login": "reviewer"}})
        saved_pr = {**self.pr, "conversation_comments": self.comments, "review_comments": []}
        self.state = {"owner/repo#1": {"watch": True,
            "deferred_revision": r.deferred_revision(saved_pr),
            "round_six": {"completed": True, "round": 6, "comment_id": 1234}}}
        before = copy.deepcopy(self.state)
        self.run_review()
        self.pr["head"]["sha"] = "c" * 40
        self.run_review()
        self.assertFalse(self.actions)
        self.assertFalse(self.posts)
        self.assertEqual(self.state, before)
        self.assertEqual(len(self.comments), 1)
        self.assertTrue(self.pr["draft"])
        record = self.state["owner/repo#1"]["round_six"]
        self.assertNotIn("reviewed_revision", record)
        self.assertEqual(record["comment_id"], 1234)
        self.evaluate.assert_not_called()

    def test_legacy_draft_is_not_approved_until_marked_ready(self):
        self.pr["draft"] = True
        self.state = {"owner/repo#1": {"watch": True, "round_six": {"completed": True, "round": 6}}}
        self.result = copy.deepcopy(CLEAN)
        self.run_review()
        self.assertTrue(self.pr["draft"])
        self.assertFalse(self.actions)
        self.assertFalse(self.comments)
        self.assertFalse(self.posts)
        self.evaluate.assert_not_called()
        self.pr["draft"] = False
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "APPROVE", "sms", "sms"])
        self.evaluate.assert_called_once()

    def test_legacy_draft_never_runs_a_corrective_assessment_while_draft(self):
        self.pr["draft"] = True
        self.state = {"owner/repo#1": {"watch": True, "round_six": {"completed": True, "round": 6}}}
        self.result = {**CLEAN, "complete": False}
        self.run_review()
        self.run_review()
        self.assertFalse(self.actions)
        self.assertFalse(self.posts)
        self.evaluate.assert_not_called()

    def test_draft_stays_tracked_and_notice_is_not_repeated_after_restart(self):
        self.run_review()
        self.state = json.loads((self.root / "state.json").read_text())
        self.run_review()
        self.pr["head"]["sha"] = "c" * 40
        self.run_review()
        self.assertTrue(self.state["owner/repo#1"]["watch"])
        self.assertTrue(self.state["owner/repo#1"]["round_six"]["completed"])
        self.assertEqual(self.actions.count("comment"), 1)
        self.assertEqual(self.actions.count("draft"), 1)
        self.evaluate.assert_called_once()

    def test_author_ready_action_resumes_review_without_immediate_redrafting(self):
        self.run_review()
        self.result = copy.deepcopy(CLEAN)
        self.pr["draft"] = False
        self.pr["head"]["sha"] = "c" * 40
        self.run_review()
        self.assertEqual(self.evaluate.call_count, 2)
        self.assertFalse(self.pr["draft"])
        self.assertTrue(self.state["owner/repo#1"]["round_six"]["completed"])
        self.assertEqual(self.actions.count("draft"), 1)
        self.assertEqual(self.actions.count("comment"), 1)
        self.assertEqual(self.posts[-1][0], "repos/owner/repo/pulls/1/reviews")

    def test_lost_comment_response_recovers_from_own_remote_marker(self):
        self.lost_comment_response = True
        with self.assertRaisesRegex(RuntimeError, "lost comment response"):
            self.run_review()
        self.assertNotIn("sms", self.actions)
        self.state = json.loads((self.root / "state.json").read_text())
        self.assertTrue(self.state["owner/repo#1"]["round_six"]["pending"])
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "draft", "comment", "sms"])
        self.assertTrue(self.state["owner/repo#1"]["round_six"]["completed"])
        self.evaluate.assert_called_once()

    def test_lost_review_response_recovers_notice_without_second_model_call(self):
        self.lost_review_response = True
        with self.assertRaisesRegex(RuntimeError, "lost review response"):
            self.run_review()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES"])
        self.state = json.loads((self.root / "state.json").read_text())
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "draft", "comment", "sms"])
        self.assertIn("round **6**", self.comments[0]["body"])
        self.assertIn("5 distinct commits", self.comments[0]["body"])
        self.evaluate.assert_called_once()

    def test_lost_draft_response_retries_comment_without_converting_twice(self):
        self.lost_draft_response = True
        with self.assertRaisesRegex(RuntimeError, "lost draft response"):
            self.run_review()
        self.assertNotIn("sms", self.actions)
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "draft", "comment", "sms"])
        self.evaluate.assert_called_once()

    def test_deferred_notice_recovers_lost_draft_response_without_reassessment(self):
        self.result = {**CLEAN, "complete": False}
        self.lost_draft_response = True
        with self.assertRaisesRegex(RuntimeError, "lost draft response"):
            self.run_review()
        self.state = json.loads((self.root / "state.json").read_text())
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "draft", "comment", "sms"])
        self.evaluate.assert_called_once()

    def test_deferred_notice_recovers_lost_comment_response_without_duplicate(self):
        self.result = {**CLEAN, "complete": False}
        self.lost_comment_response = True
        with self.assertRaisesRegex(RuntimeError, "lost comment response"):
            self.run_review()
        self.state = json.loads((self.root / "state.json").read_text())
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "draft", "comment", "sms"])
        self.assertEqual(len(self.comments), 1)
        self.evaluate.assert_called_once()

    def test_conversion_permission_failure_does_not_post_notice_or_sms(self):
        self.forbid_draft = True
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            self.run_review()
        self.assertFalse(self.pr["draft"])
        self.assertFalse(self.comments)
        self.assertNotIn("sms", self.actions)
        self.evaluate.assert_called_once()

    def test_stale_closed_or_unassigned_pr_is_not_changed(self):
        for changes in ({"head": {"sha": "c" * 40}}, {"state": "closed"}, {"draft": True},
                        {"requested_reviewers": []}):
            with self.subTest(changes=changes):
                fresh = {**self.pr, **changes}
                with patch.object(r, "api", side_effect=[self.pr, fresh]):
                    self.run_review()
                self.assertFalse(self.posts)
                self.assertNotIn("sms", self.actions)

    def test_changed_head_after_conversion_waits_until_ready_without_reviewing_draft(self):
        self.changed_after_draft = True
        self.run_review()
        self.assertTrue(self.pr["draft"])
        self.assertFalse(self.comments)
        self.assertNotIn("sms", self.actions)
        self.run_review()
        self.assertEqual(self.actions.count("draft"), 1)
        self.assertEqual(self.actions.count("comment"), 0)
        self.evaluate.assert_called_once()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "draft"])

    def test_account_change_prevents_draft_and_comment(self):
        self.login = "other-account"
        with self.assertRaisesRegex(RuntimeError, "account changed"):
            self.run_review()
        self.assertFalse(self.posts)
        self.assertNotIn("sms", self.actions)

    def test_account_change_after_draft_stops_the_comment(self):
        self.changed_account_after_draft = True
        with self.assertRaisesRegex(RuntimeError, "account changed"):
            self.run_review()
        self.assertTrue(self.pr["draft"])
        self.assertFalse(self.comments)
        self.assertNotIn("sms", self.actions)

    def test_revoked_repository_scope_prevents_mutations(self):
        fresh = copy.deepcopy(self.pr)
        def api(endpoint, **kwargs):
            if endpoint == "repos/owner/repo/pulls/1":
                r.CONFIG["repositories"] = []
                return fresh
            return self.api(endpoint, **kwargs)
        history = r.review_history(self.reviews, "reviewer", r.MARKER)
        with patch.object(r, "api", side_effect=api), self.assertRaisesRegex(RuntimeError, "scope"):
            r.apply_round_six("owner/repo", 1, self.pr, self.reviews, self.state, history)
        self.assertFalse(self.posts)

    def test_other_users_marker_cannot_suppress_the_notice(self):
        self.comments.append({"id": 100, "body": r.ROUND_SIX_MARKER, "user": {"login": "someone-else"}})
        self.run_review()
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "draft", "comment", "sms"])

    def test_remote_marker_after_author_ready_is_recovered_without_redrafting(self):
        self.comments.append({"id": 100, "body": r.ROUND_SIX_MARKER, "user": {"login": "reviewer"}})
        self.state = {"owner/repo#1": {"watch": True, "round_six": {"pending": True, "round": 6}}}
        self.run_review()
        self.assertFalse(self.pr["draft"])
        self.assertEqual(self.actions, ["evaluate", "REQUEST_CHANGES", "sms"])
        self.assertEqual(self.state["owner/repo#1"]["round_six"]["comment_id"], 100)

    def test_round_five_reviews_normally(self):
        self.reviews = self.reviews[:4]
        self.run_review()
        self.evaluate.assert_called_once()
        self.assertFalse(self.pr["draft"])
        self.assertFalse(self.comments)

    def test_already_reviewed_fifth_commit_does_not_start_a_sixth_round(self):
        self.pr["head"]["sha"] = self.reviews[-1]["commit_id"]
        self.run_review()
        self.assertFalse(self.posts)
        self.evaluate.assert_not_called()

    def test_human_reviews_and_duplicate_commit_reviews_do_not_create_round_six(self):
        self.reviews = [*[review(n, sha="f" * 40) for n in range(1, 6)],
                        *[review(n, user={"login": "human"}) for n in range(6, 12)]]
        self.run_review()
        self.evaluate.assert_called_once()
        self.assertFalse(self.pr["draft"])
        self.assertFalse(self.comments)

    def test_dry_run_previews_a_separate_notice_without_writes_or_sms(self):
        self.run_review(dry_run=True)
        self.assertFalse(self.posts)
        self.assertFalse(self.state)
        self.assertFalse((self.root / "state.json").exists())
        self.assertFalse(self.notifier.mock_calls)
        self.evaluate.assert_called_once()

    def test_missing_finding_evidence_fails_before_converting(self):
        self.reviews[0]["body"] = r.MARKER
        with self.assertRaisesRegex(ValueError, "no readable finding"):
            self.run_review()
        self.assertFalse(self.posts)
        self.notifier.observe.assert_not_called()

    def test_quoted_findings_do_not_tag_unrelated_users(self):
        self.reviews[-1]["body"] = self.reviews[-1]["body"].replace(
            "Tenant access is not checked.", "Tenant access is not checked. @unrelated-team")
        self.run_review()
        body = self.comments[0]["body"]
        self.assertIn("@author", body)
        self.assertNotIn("@unrelated-team", body)

    def test_long_issue_excerpts_are_visibly_truncated(self):
        self.reviews[-1]["body"] = self.reviews[-1]["body"].replace(
            "Tenant access is not checked.", "Recorded issue with repeated detail. " * 20)
        self.run_review()
        lines = self.comments[0]["body"].splitlines()
        excerpt = next(line for line in lines if "Recorded issue with repeated detail." in line)
        self.assertIn("...", excerpt)
        self.assertLess(len(excerpt), 900)

    def test_policy_can_be_disabled_without_affecting_review_submission(self):
        r.CONFIG["draft_at_round_six"] = False
        self.run_review()
        self.evaluate.assert_called_once()
        self.assertFalse(self.pr["draft"])
        self.assertFalse(self.comments)


if __name__ == "__main__":
    unittest.main()
