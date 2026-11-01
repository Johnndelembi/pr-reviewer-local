import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import reviewer as r
from review_policy import apply_policy, prepare_history, review_history


def finding(origin="original", **values):
    return {"severity": "Must fix", "file": "auth.py", "line": 1,
            "problem": "Tenant access is not checked.", "fix": "Check tenant ownership.",
            "impact": "Private data is exposed.", "origin": origin, "prior_finding_id": None,
            "origin_evidence": "The changed authorization path permits a different tenant.",
            "change_file": None, "change_line": None, "change_side": None, **values}


def result(findings=(), statuses=()):
    return {"complete": True, "summary": "Verifies tenant access.", "limitations": "Static test fixture.",
            "findings": list(findings), "prior_finding_statuses": list(statuses)}


def review(number, sha=None, **values):
    sha = sha or f"{number:040x}"
    return {"id": number, "commit_id": sha, "state": "CHANGES_REQUESTED",
            "user": {"login": "reviewer"},
            "body": r.review_body(result([finding()]), sha)["body"], **values}


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / "head").mkdir()
        (self.work / "previous-review").mkdir()

    def history(self, rounds=2):
        history = review_history([review(n) for n in range(1, rounds + 1)], "reviewer", r.MARKER)
        history["previous_snapshot"] = "previous-review/"
        return history

    def statuses(self, history, **changes):
        return [{"id": item["id"], "status": changes.get(item["id"], "resolved"),
                 "evidence": "The current auth.py:1 checks the tenant."}
                for item in history["prior_findings"]]

    def apply(self, findings, history=None, statuses=None):
        history = history or self.history()
        data = result(findings, self.statuses(history) if statuses is None else statuses)
        apply_policy(data, history, self.work, "f" * 40)
        return r.review_body(data, "f" * 40), data

    def test_history_counts_only_agent_change_request_turns_and_survives_dismissal(self):
        reviews = [review(1), review(2, state="DISMISSED"), review(3, sha=f"{2:040x}"),
                   review(4, user={"login": "other"}), review(5, body="Manual review"),
                   review(6, state="COMMENTED"), review(7, state="APPROVED", body=r.MARKER)]
        history = review_history(reviews, "REVIEWER", r.MARKER)
        self.assertEqual(history["request_changes_rounds"], 2)
        self.assertEqual(history["round"], 3)
        self.assertEqual(history["previous_head"], f"{7:040x}")
        self.assertEqual(len(history["prior_findings"]), 3)

    def test_old_and_formatted_reviews_preserve_rounds_and_exclude_validation_footer(self):
        old_body = ("1. **Must fix** — `auth.py:1`\n"
                    "   - Problem: Tenant access is not checked.\n"
                    "   - Fix: Check tenant ownership.\n"
                    "   - If skipped: Private data is exposed.\n\n"
                    "Validation: Legacy validation details.\nLimits: Static review.\n" + r.MARKER)
        formatted = review(2)
        history = review_history([review(1, body=old_body), formatted], "reviewer", r.MARKER)
        self.assertEqual(history["request_changes_rounds"], 2)
        self.assertEqual(history["round"], 3)
        self.assertEqual(len(history["prior_findings"]), 2)
        for finding in history["prior_findings"]:
            self.assertEqual((finding["file"], finding["line"]), ("auth.py", 1))
            self.assertIn("Tenant access is not checked.", finding["text"])
            self.assertNotIn("Validation:", finding["text"])
            self.assertNotIn(r.MARKER, finding["text"])

    def test_first_two_rounds_include_every_severity_together(self):
        for round_count in (0, 1):
            history = self.history(round_count)
            findings = [finding(severity=severity, problem=f"Defect {index}")
                        for index, severity in enumerate(("Must fix", "Should fix", "Nice to have"))]
            payload, _ = self.apply(findings, history)
            self.assertEqual(payload["event"], "REQUEST_CHANGES")
            for item in findings:
                self.assertIn(item["problem"], payload["body"])

    def test_third_round_original_misses_are_comments_at_every_severity(self):
        for severity in ("Must fix", "Should fix", "Nice to have"):
            payload, data = self.apply([finding(severity=severity)])
            self.assertEqual(payload["event"], "COMMENT")
            self.assertIn("Reviewer omissions", payload["body"])
            self.assertNotIn("LGTM", payload["body"])
            self.assertEqual(data["findings"][0]["origin"], "late_discovery")

    def test_existing_five_round_history_does_not_reset_the_limit(self):
        payload, _ = self.apply([finding()], self.history(5))
        self.assertEqual(payload["event"], "COMMENT")
        self.assertIn("Review round 6", payload["body"])

    def test_added_line_regression_can_block_after_round_two(self):
        (self.work / "previous-review/auth.py").write_text("authorize()\nreturn_data()\n")
        (self.work / "head/auth.py").write_text("skip_authorize()\nreturn_data()\n")
        payload, _ = self.apply([finding("regression", change_file="auth.py", change_line=1, change_side="head")])
        self.assertEqual(payload["event"], "REQUEST_CHANGES")

    def test_removed_authorization_can_block_after_round_two(self):
        (self.work / "previous-review/auth.py").write_text("authorize()\nreturn_data()\n")
        (self.work / "head/auth.py").write_text("return_data()\n")
        payload, _ = self.apply([finding("regression", change_file="auth.py", change_line=1, change_side="previous")])
        self.assertEqual(payload["event"], "REQUEST_CHANGES")

    def test_new_commit_or_rebase_with_unchanged_source_cannot_claim_regression(self):
        for root in ("previous-review", "head"):
            (self.work / root / "auth.py").write_text("existing_bug()\n")
        payload, _ = self.apply([finding("regression", change_file="auth.py", change_line=1, change_side="head")])
        self.assertEqual(payload["event"], "COMMENT")

    def test_editing_a_nearby_line_does_not_make_an_existing_line_new(self):
        (self.work / "previous-review/auth.py").write_text("existing_bug()\nlabel = 'old'\n")
        (self.work / "head/auth.py").write_text("existing_bug()\nlabel = 'new'\n")
        payload, _ = self.apply([finding("regression", change_file="auth.py", change_line=1, change_side="head")])
        self.assertEqual(payload["event"], "COMMENT")

    def test_invalid_or_missing_regression_evidence_cannot_block(self):
        for path in ("../secret", "/etc/passwd", "..\\secret", None):
            payload, _ = self.apply([finding("regression", change_file=path, change_line=1, change_side="head")])
            self.assertEqual(payload["event"], "COMMENT")

    def test_known_unresolved_issue_keeps_its_identity_after_many_rounds(self):
        history = self.history(5)
        identifier = history["prior_findings"][0]["id"]
        statuses = self.statuses(history, **{identifier: "unresolved"})
        payload, _ = self.apply([finding("unresolved", prior_finding_id=identifier)], history, statuses)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        followup = review(99, sha="f" * 40, body=payload["body"])
        rebuilt = review_history([*[review(n) for n in range(1, 6)], followup], "reviewer", r.MARKER)
        self.assertEqual(len(rebuilt["prior_findings"]), 5)
        self.assertEqual(next(f for f in rebuilt["prior_findings"] if f["id"] == identifier)["review_id"], 99)

    def test_late_comment_cannot_be_laundered_into_an_unresolved_blocker(self):
        payload, data = self.apply([finding()])
        history = review_history([review(1), review(2), review(3, state="COMMENTED", body=payload["body"])],
                                 "reviewer", r.MARKER)
        late_id = data["_review_policy"]["findings"][0]["id"]
        with self.assertRaisesRegex(ValueError, "earlier blocking finding"):
            self.apply([finding("unresolved", prior_finding_id=late_id)], history)

    def test_mixed_review_keeps_late_comments_separate_from_valid_requests(self):
        history = self.history()
        identifier = history["prior_findings"][0]["id"]
        payload, _ = self.apply([finding("unresolved", prior_finding_id=identifier), finding(problem="Late omission")],
                                history, self.statuses(history, **{identifier: "unresolved"}))
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertIn("Reviewer omissions", payload["body"])
        rebuilt = review_history([review(1), review(2), review(3, sha="f" * 40, body=payload["body"])],
                                 "reviewer", r.MARKER)
        self.assertEqual(len(rebuilt["prior_findings"]), 2)

    def test_every_earlier_finding_must_have_a_resolution_before_approval(self):
        with self.assertRaisesRegex(ValueError, "Every earlier finding"):
            self.apply([], statuses=[])
        history = self.history()
        identifier = history["prior_findings"][0]["id"]
        with self.assertRaisesRegex(ValueError, "All unresolved"):
            self.apply([], history, self.statuses(history, **{identifier: "unresolved"}))

    def test_unverified_prior_finding_prevents_approval(self):
        history = self.history()
        identifier = history["prior_findings"][0]["id"]
        payload, data = self.apply([], history, self.statuses(history, **{identifier: "unverified"}))
        self.assertFalse(data["complete"])
        self.assertIsNone(payload)

    def test_resolving_all_previous_findings_can_approve(self):
        payload, _ = self.apply([])
        self.assertEqual(payload["event"], "APPROVE")

    def test_comment_only_followup_is_deduplicated_but_human_comments_are_not(self):
        payload, _ = self.apply([finding()])
        posted = review(3, sha="f" * 40, state="COMMENTED", body=payload["body"])
        self.assertTrue(r.already_reviewed([posted], "reviewer", "f" * 40))
        self.assertFalse(r.already_reviewed([{**posted, "body": "Human comment"}], "reviewer", "f" * 40))

    def test_renamed_submodule_uses_its_previous_source_for_origin_verification(self):
        (self.work / "previous-review/old").mkdir()
        (self.work / "head/new").mkdir()
        (self.work / "previous-review/old/auth.py").write_text("existing_bug()\n")
        (self.work / "head/new/auth.py").write_text("existing_bug()\n")
        history = self.history()
        history["previous_paths"] = {"new": "old"}
        payload, _ = self.apply([finding("regression", change_file="new/auth.py", change_line=1, change_side="head")], history)
        self.assertEqual(payload["event"], "COMMENT")
        payload, _ = self.apply([finding("regression", change_file="old/auth.py", change_line=1, change_side="previous")], history)
        self.assertEqual(payload["event"], "COMMENT")

    def test_renamed_file_is_compared_with_previous_content_before_claiming_a_regression(self):
        history = self.history()
        (self.work / "previous-review").rmdir()
        (self.work / "head/renamed.py").write_text("existing_bug()\n")
        def extract(data, target):
            (target / "original.py").write_text("existing_bug()\n")
            return []
        prepare_history(self.work, "owner/repo", history, {}, api=Mock(), gh=Mock(return_value=b"archive"),
                        extract_snapshot=extract)
        self.assertEqual(history["previous_paths"]["renamed.py"], "original.py")
        for side, path in (("head", "renamed.py"), ("previous", "original.py")):
            payload, _ = self.apply([finding("regression", change_file=path, change_line=1, change_side=side)], history)
            self.assertEqual(payload["event"], "COMMENT")

    def test_prepare_history_fetches_exact_previous_snapshot_and_builds_delta(self):
        history = self.history()
        (self.work / "previous-review").rmdir()
        (self.work / "head/auth.py").write_text("new_source()\n")
        gh, api = Mock(return_value=b"archive"), Mock()
        def extract(data, target):
            (target / "auth.py").write_text("old_source()\n")
            return []
        prepare_history(self.work, "owner/repo", history, {}, api=api, gh=gh, extract_snapshot=extract)
        gh.assert_called_once_with("api", "--hostname", "github.com", "repos/owner/repo/tarball/" + history["previous_head"])
        api.assert_not_called()
        delta = (self.work / history["incremental_diff"]).read_text()
        self.assertIn("-old_source()", delta)
        self.assertIn("+new_source()", delta)

    def test_previous_submodule_snapshots_use_prior_gitlinks_and_map_moved_paths(self):
        history = self.history()
        (self.work / "previous-review").rmdir()
        (self.work / "head/new").mkdir()
        (self.work / "head/new/auth.py").write_text("new_source()\n")
        sha = "b" * 40
        gh = Mock(return_value=b"archive")
        api = Mock(return_value={"tree": [{"path": "old", "sha": sha, "mode": "160000"}]})
        def extract(data, target):
            if target.name == "previous-review":
                (target / ".gitmodules").write_text('[submodule "shared"]\npath = old\nurl = git@github.com:owner/shared.git\n')
            else:
                (target / "auth.py").write_text("old_source()\n")
            return []
        prepare_history(self.work, "owner/repo", history, {"head/new": {"repository": "owner/shared"}},
                        api=api, gh=gh, extract_snapshot=extract)
        self.assertEqual(history["previous_paths"], {"new": "old"})
        gh.assert_any_call("api", "--hostname", "github.com", "repos/owner/shared/tarball/" + sha)
        self.assertIn("old_source", (self.work / "previous-review/old/auth.py").read_text())

    def test_evaluator_enforces_policy_before_returning_a_postable_result(self):
        history = self.history()
        context = {"review_history": history, "companion_evidence": {"pull_requests": [], "unavailable": []}}
        data = result([finding()], self.statuses(history))
        (self.work / "work").mkdir()
        (self.work / "review-prompt.md").write_text("Trusted prompt")
        (self.work / "review-schema.json").write_text(
            (Path(__file__).parent / "review-schema.json").read_text())
        def command(args, **kwargs):
            Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(data))
        config = {"skill_files": [], "codex": "codex", "model": "model", "reasoning": "xhigh"}
        with patch.object(r, "ROOT", self.work), patch.object(r, "CONFIG", config), \
                patch.object(r, "prepare_context", return_value=context), \
                patch.object(r, "run_checks", return_value={"status": "passed", "targets": []}), \
                patch.object(r, "command", side_effect=command):
            reviewed = r.evaluate("owner/repo", 1, {"head": {"sha": "f" * 40}, "base": {"sha": "e" * 40}}, [])
        self.assertEqual(r.review_body(reviewed, "f" * 40)["event"], "COMMENT")


if __name__ == "__main__":
    unittest.main()
