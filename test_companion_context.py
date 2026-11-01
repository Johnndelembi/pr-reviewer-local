import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from companion_context import collect_companion_context


ENGINE = "iPFSoftwares/notify-ai-engine"
BRIDGE = "iPFSoftwares/notify-voice-ai-bridge"
VOICE = "iPFSoftwares/notify-voice-service"
SOURCES = {"services/ai/engine": ENGINE, "services/voice/ai-bridge": BRIDGE,
           "services/voice/voice": VOICE}


class CompanionContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / "head" / "docs").mkdir(parents=True)
        self.api = Mock()
        self.gh = Mock(return_value=b"archive")
        self.extract = Mock(side_effect=lambda data, target: (target / "source.txt").write_text("source") and [])

    def collect(self, text, body="", **kwargs):
        (self.work / "head/docs/task.md").write_text(text)
        return collect_companion_context(
            self.work, [{"filename": "docs/task.md"}], {"body": body}, sources=SOURCES,
            api=self.api, gh=self.gh, extract_snapshot=self.extract,
            log=logging.getLogger("test"), **kwargs)

    def test_pr113_citations_load_all_five_exact_commits_in_distinct_directories(self):
        engine = "db56d46d51e634237ada2283aded73818326030d"
        bridge = "3343bb809ac68badc7ab6ff4f5a4071f306cf9b9"
        bridge_fix = "09cedd4b1d0c527110e9f27a9b483918bc410e64"
        voice = "c564268733899a94df8964d38eca2592ee342d60"
        voice_fix = "da75942747b716803ba1c440b5c956a112955e2c"
        heads = {f"repos/{ENGINE}/pulls/77": engine, f"repos/{BRIDGE}/pulls/4": bridge_fix,
                 f"repos/{VOICE}/pulls/34": voice_fix}
        self.api.side_effect = lambda endpoint: (
            {"head": {"sha": heads[endpoint]}} if endpoint in heads else
            {"sha": {"09cedd4": bridge_fix, "da75942": voice_fix}[endpoint.rsplit("/", 1)[-1]]})
        result = self.collect(
            f"- [ ] AI Engine: send when the Bridge declared the capability. Companion PR #77 commit `{engine}`.\n"
            f"- [ ] Bridge: declare capability to the Engine. Companion PR #4 commit `{bridge}`. Review fixes in `09cedd4`.\n"
            f"- [ ] Voice: retain Bridge state. Companion PR #34 commit `{voice}`. Review fixes in `da75942`.\n\n"
            f"The tested heads were AI Engine `{engine}`, Bridge `{bridge}`, and Voice `{voice}`.\n\n"
            "Repeat the scenario in notify-voice-mock-provider (`scenarios/barge-in-baseline.mjs`).",
            f"Companion PRs: https://github.com/{ENGINE}/pull/77, https://github.com/{BRIDGE}/pull/4, "
            f"https://github.com/{VOICE}/pull/34.")
        self.assertEqual({(item["repository"], item["sha"]) for item in result["snapshots"]},
                         {(ENGINE, engine), (BRIDGE, bridge), (BRIDGE, bridge_fix),
                          (VOICE, voice), (VOICE, voice_fix)})
        self.assertEqual(self.gh.call_count, 5)
        self.assertEqual(self.api.call_count, 5)
        self.assertEqual(len(result["pull_requests"]), 3)
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["evidence"][0]["status"], "author_reported")
        for item in result["snapshots"]:
            self.assertTrue((self.work / item["path"] / "source.txt").is_file())
            self.assertTrue(item["author_reported"])
            self.assertFalse(item["pinned"])
            self.assertTrue(item["provenance"])

    def test_explicit_commit_url_and_short_ref_share_one_snapshot_without_changing_pins(self):
        sha = "a" * 40
        self.api.return_value = {"sha": sha}
        pinned = self.work / "head/services/ai/engine"
        pinned.mkdir(parents=True)
        (pinned / "source.txt").write_text("pinned")
        result = self.collect(f"See https://github.com/{ENGINE}/commit/{sha}.\n\nEngine commit `aaaaaaa`.")
        self.assertEqual(len(result["snapshots"]), 1)
        self.assertEqual(len(result["snapshots"][0]["provenance"]), 2)
        self.assertEqual(self.gh.call_count, 1)
        self.assertEqual((pinned / "source.txt").read_text(), "pinned")

    def test_untrusted_repositories_and_uncited_latest_heads_are_never_fetched(self):
        result = self.collect(
            "See https://github.com/unknown/private/commit/" + "b" * 40
            + ".\n\nCompanion PR: https://github.com/unknown/private/pull/77.\n\n"
            "Use the latest Engine branch.")
        self.api.assert_not_called()
        self.gh.assert_not_called()
        self.assertEqual(result["snapshots"], [])
        self.assertEqual(result["unavailable"][0]["reason"], "repository is not approved")

    def test_explicit_context_repository_allows_cited_immutable_commit(self):
        extra = "iPFSoftwares/notify-voice-mock-provider"
        sha = "c" * 40
        result = self.collect(f"Harness: https://github.com/{extra}/commit/{sha}.",
                              context_repositories=[extra])
        self.api.assert_not_called()
        self.assertEqual(result["snapshots"][0]["repository"], extra)
        self.assertEqual(result["snapshots"][0]["sha"], sha)

    def test_short_sha_must_resolve_to_a_matching_full_sha(self):
        self.api.return_value = {"sha": "b" * 40}
        result = self.collect("Engine review fix `aaaaaaa`.")
        self.gh.assert_not_called()
        self.assertEqual(result["snapshots"], [])
        self.assertIn("does not match", result["unavailable"][0]["reason"])

    def test_snapshot_budget_is_checked_before_any_download(self):
        text = "\n\n".join(f"Engine `{index:040x}`." for index in range(9))
        with self.assertRaisesRegex(ValueError, "eight companion"):
            self.collect(text)
        self.gh.assert_not_called()

    def test_comment_companion_links_load_trusted_source_with_provenance(self):
        self.api.return_value = {"head": {"sha": "d" * 40}}
        result = self.collect("", comments=[{
            "id": 42, "html_url": "https://github.com/owner/repo/pull/1#issuecomment-42",
            "body": f"Companion: https://github.com/{BRIDGE}/pull/4",
        }])
        self.assertEqual(result["pull_requests"], [{"repository": BRIDGE, "number": 4, "sha": "d" * 40}])
        self.assertIn("issuecomment-42", str(result["snapshots"][0]["provenance"]))
        self.api.assert_called_once_with(f"repos/{BRIDGE}/pulls/4")

    def test_comment_links_do_not_expand_repository_allowlist(self):
        result = self.collect("", comments=[{"id": 42, "body": "https://github.com/unknown/private/pull/4"}])
        self.assertEqual(result["pull_requests"], [])
        self.api.assert_not_called()
        self.gh.assert_not_called()

    def test_oversized_comment_is_rejected_before_fetching_companions(self):
        with self.assertRaisesRegex(ValueError, "per-comment limit"):
            self.collect("", comments=[{"id": 1, "body": "a" * 1_000_001}])
        self.api.assert_not_called()
        self.gh.assert_not_called()

    def test_pull_number_reference_uses_trusted_link_in_pr_description(self):
        sha = "d" * 40
        self.api.return_value = {"head": {"sha": sha}}
        result = self.collect(f"Companion PR #77 tested commit `{sha}`.",
                              f"https://github.com/{ENGINE}/pull/77")
        self.assertEqual(result["snapshots"][0]["repository"], ENGINE)
        self.assertEqual(self.gh.call_count, 1)
        self.assertEqual(result["unavailable"], [])

    def test_linked_pr_head_and_historical_commit_are_separate_exact_snapshots(self):
        historical, current = "a" * 40, "b" * 40
        self.api.return_value = {"head": {"sha": current}}
        result = self.collect(
            f"Engine tested commit `{historical}`.\n\n"
            f"Pairs with notify-ai-engine#78 and {ENGINE}#78.\n\n"
            f"https://github.com/{ENGINE}/pull/78")
        self.api.assert_called_once_with(f"repos/{ENGINE}/pulls/78")
        self.assertEqual(result["pull_requests"], [{"repository": ENGINE, "number": 78, "sha": current}])
        self.assertEqual({item["sha"] for item in result["snapshots"]}, {historical, current})
        self.assertEqual(self.gh.call_count, 2)
        current_snapshot = next(item for item in result["snapshots"] if item["sha"] == current)
        self.assertTrue(all(p["kind"] == "pull_request_head" for p in current_snapshot["provenance"]))
        self.assertEqual(result["unavailable"], [])

    def test_unapproved_shorthand_and_malformed_pr_url_do_not_authorize_reads(self):
        result = self.collect(
            f"other/notify-ai-engine#78, unknown/private#7, https://github.com/{ENGINE}/pull/notanumber")
        self.api.assert_not_called()
        self.gh.assert_not_called()
        self.assertEqual(result["pull_requests"], [])

    def test_unavailable_pr_remains_identifiable_for_retry(self):
        self.api.side_effect = RuntimeError("network unavailable")
        result = self.collect("Pairs with notify-ai-engine#78.")
        self.assertEqual(result["pull_requests"], [{"repository": ENGINE, "number": 78, "sha": None}])
        self.assertEqual(result["unavailable"][0]["reason"], "network unavailable")
        self.gh.assert_not_called()

    def test_invalid_pr_head_cannot_be_used_in_archive_request(self):
        self.api.return_value = {"head": {"sha": "../../outside"}}
        result = self.collect("Pairs with notify-ai-engine#78.")
        self.assertEqual(result["unavailable"][0]["reason"], "Invalid companion PR head SHA")
        self.gh.assert_not_called()

    def test_companion_pr_limit_is_checked_before_api_calls(self):
        with self.assertRaisesRegex(ValueError, "eight companion PR"):
            self.collect("\n".join(f"notify-ai-engine#{n}" for n in range(1, 10)))
        self.api.assert_not_called()
        self.gh.assert_not_called()

    def test_pr_heads_share_existing_eight_snapshot_budget(self):
        self.api.return_value = {"head": {"sha": "f" * 40}}
        text = "\n\n".join(f"Engine `{index:040x}`." for index in range(8))
        with self.assertRaisesRegex(ValueError, "eight companion commit"):
            self.collect(text + "\n\nnotify-ai-engine#78")
        self.gh.assert_not_called()

    def test_configured_repository_cannot_escape_supplemental_directory(self):
        with self.assertRaisesRegex(ValueError, "Invalid companion"):
            self.collect("", context_repositories=["owner/.."])
        self.gh.assert_not_called()

    def test_unknown_repository_name_does_not_inherit_previous_engine_alias(self):
        sha = "f" * 40
        result = self.collect(f"AI Engine `{sha}`, and sibling `notify-africa-infra` `32313a5` checkouts.")
        self.assertEqual(self.gh.call_count, 1)
        self.api.assert_not_called()
        self.assertEqual(result["snapshots"][0]["sha"], sha)
        self.assertEqual(result["unavailable"][0]["ref"], "32313a5")

    def test_numeric_configuration_value_is_not_treated_as_a_commit(self):
        result = self.collect("Engine timeout is 10000000 ms.")
        self.api.assert_not_called()
        self.gh.assert_not_called()
        self.assertEqual(result["snapshots"], [])

    def test_numeric_short_ref_is_allowed_in_an_explicit_commit_url(self):
        sha = "1234567" + "a" * 33
        self.api.return_value = {"sha": sha}
        result = self.collect(f"https://github.com/{ENGINE}/commit/1234567")
        self.assertEqual(result["snapshots"][0]["sha"], sha)

    def test_planned_harness_scope_is_a_reference_not_a_missing_dependency(self):
        result = self.collect("Future scope: repeat the harness once provider credentials exist.")
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["evidence"][0]["status"], "author_reported")
        self.assertEqual(result["evidence"][0]["execution_verification"], "not_independently_verified")
        self.assertEqual(result["evidence"][0]["artifact_collection"], "not_performed_by_this_loader")
        self.gh.assert_not_called()

    def test_failed_extraction_removes_partial_supplemental_snapshot(self):
        def extract(data, target):
            (target / "partial.txt").write_text("partial")
            raise ValueError("bad archive")

        self.extract.side_effect = extract
        sha = "e" * 40
        result = self.collect(f"Engine `{sha}`.")
        self.assertFalse((self.work / "supplemental/notify-ai-engine" / sha).exists())
        self.assertEqual(result["snapshots"], [])
        self.assertEqual(result["unavailable"][0]["reason"], "bad archive")


if __name__ == "__main__":
    unittest.main()
