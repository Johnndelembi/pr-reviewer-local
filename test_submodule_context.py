import logging
from pathlib import Path
import tempfile
import unittest

from submodule_context import expand_referenced_submodules, select_submodules, submodule_sources


class SubmoduleContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.snapshots = {side: self.work / side for side in ("base", "head")}
        for root in self.snapshots.values():
            root.mkdir()

    def modules(self, side, mapping):
        content = "".join(f'[submodule "{path}"]\npath = {path}\nurl = https://github.com/{repo}.git\n'
                          for path, repo in mapping.items())
        (self.snapshots[side] / ".gitmodules").write_text(content)

    def select(self, base, head, files, diff=""):
        return select_submodules(base, head, files, diff, self.snapshots)

    def test_grouped_compose_and_env_expand_exact_services_across_moved_paths(self):
        base = {"services/notify-ai-engine": "org/ai", "services/notify-voice-service": "org/voice",
                "services/notify-voice-ai-bridge": "org/bridge", "services/auth": "org/auth"}
        head = {"services/ai/engine": "org/ai", "services/voice/voice": "org/voice",
                "services/voice/ai-bridge": "org/bridge", "services/auth": "org/auth"}
        selected, reasons, skipped = self.select(base, head, [
            {"filename": "services/ai/compose.yml"}, {"filename": "services/voice/.env.example"}])
        self.assertEqual(set(selected["head"]), set(head) - {"services/auth"})
        self.assertEqual(set(selected["base"]), set(base) - {"services/auth"})
        self.assertIn("service group services/ai/compose.yml", reasons["org/ai"])
        self.assertEqual(skipped, [])

    def test_changed_paths_and_rename_previous_filename_select_both_sides(self):
        base = {"legacy/engine": "org/engine"}
        head = {"services/ai/engine": "org/engine"}
        selected, _, _ = self.select(base, head, [
            {"filename": "docs/engine.txt", "previous_filename": "legacy/engine/config.txt"}])
        self.assertEqual(selected, {"base": base, "head": head})

    def test_literal_shared_build_context_and_unsafe_contexts(self):
        mapping = {"services/voice/voice": "org/voice", "services/shared/nest-shared": "org/shared",
                   "secrets": "org/secrets", "services/auth": "org/auth"}
        compose = self.snapshots["head"] / "services/voice/compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services:\n  voice:\n    build:\n      context: '../shared/nest-shared' # dependency\n"
                           "  remote:\n    build: https://github.com/unknown/repo\n"
                           "  dynamic:\n    build: ${ROOT}/services/auth\n"
                           "  escape:\n    build: ../../../secrets\n")
        selected, reasons, _ = self.select(mapping, mapping, [{"filename": "services/voice/compose.yml"}])
        self.assertEqual(set(selected["head"]), {"services/voice/voice", "services/shared/nest-shared"})
        self.assertTrue(any("build context" in reason for reason in reasons["org/shared"]))

    def test_root_compose_expands_literal_dependency(self):
        mapping = {"services/voice": "org/voice", "services/auth": "org/auth"}
        (self.snapshots["head"] / "docker-compose.yml").write_text("services:\n  voice:\n    build: ./services/voice\n")
        selected, _, _ = self.select(mapping, mapping, [{"filename": "docker-compose.yml"}])
        self.assertEqual(selected["head"], {"services/voice": "org/voice"})

    def test_compose_additional_contexts_mapping_and_list_select_shared_dependencies(self):
        mapping = {"services/voice/voice": "org/voice", "libs/nest-shared": "org/shared",
                   "libs/other-shared": "org/other", "libs/unrelated": "org/unrelated"}
        compose = self.snapshots["head"] / "services/voice/compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("services:\n  voice:\n    build:\n      context: ./voice\n"
                           "      additional_contexts:\n        nest-shared: ../../libs/nest-shared\n"
                           "    environment:\n      UNRELATED: ../../libs/unrelated\n"
                           "  other:\n    build:\n      additional_contexts:\n"
                           "        - other-shared=../../libs/other-shared\n"
                           "        - remote=https://example.com/repo\n")
        selected, reasons, _ = self.select(mapping, mapping, [{"filename": "services/voice/compose.yml"}])
        self.assertEqual(set(selected["head"]), set(mapping) - {"libs/unrelated"})
        self.assertTrue(any("build context libs/nest-shared" in reason for reason in reasons["org/shared"]))

    def test_root_context_uses_explicit_service_reference_without_selecting_all_modules(self):
        mapping = {f"services/service{index:02}": f"org/service{index:02}" for index in range(15)}
        mapping["services/notify-voice-service"] = "org/notify-voice-service"
        (self.snapshots["head"] / "compose.yml").write_text("services:\n  app:\n    build: .\n")
        selected, _, skipped = self.select(mapping, mapping, [{"filename": "compose.yml"}], "notify-voice-service")
        self.assertEqual(selected["head"], {"services/notify-voice-service": "org/notify-voice-service"})
        self.assertTrue(any("repository-wide" in reason for reason in skipped))

    def test_root_context_without_service_reference_records_ambiguity(self):
        mapping = {"services/voice": "org/voice"}
        (self.snapshots["head"] / "compose.yml").write_text("services:\n  app:\n    build: .\n")
        selected, _, skipped = self.select(mapping, mapping, [{"filename": "compose.yml"}])
        self.assertEqual(selected, {"base": {}, "head": {}})
        self.assertEqual(len(skipped), 1)
        self.assertIn("explicit service references", skipped[0])

    def test_env_change_loads_shared_dependencies_from_unchanged_sibling_compose(self):
        mapping = {"services/voice/voice": "org/voice", "libs/nest-shared": "org/shared",
                   "libs/other": "org/other", "libs/unrelated": "org/unrelated"}
        group = self.snapshots["head"] / "services/voice"
        group.mkdir(parents=True)
        (group / "compose.yml").write_text("services:\n  voice:\n    build:\n"
                                           "      additional_contexts:\n"
                                           "        nest-shared: ../../libs/nest-shared\n")
        (group / "docker-compose.extra.yaml").write_text("build: ../../libs/other\n")
        (group / "settings.yml").write_text("build: ../../libs/unrelated\n")
        nested = group / "nested"
        nested.mkdir()
        (nested / "compose.yml").write_text("build: ../../../libs/unrelated\n")
        selected, reasons, _ = self.select(mapping, mapping, [{"filename": "services/voice/.env.example"}])
        self.assertEqual(set(selected["head"]), set(mapping) - {"libs/unrelated"})
        self.assertIn("build context libs/nest-shared in services/voice/compose.yml", reasons["org/shared"])

    def test_env_sibling_compose_scan_is_bounded(self):
        group = self.snapshots["head"] / "services/voice"
        group.mkdir(parents=True)
        for index in range(17):
            (group / f"compose.{index}.yml").write_text("services: {}\n")
        with self.assertRaisesRegex(ValueError, "sixteen Compose"):
            self.select({}, {}, [{"filename": "services/voice/.env"}])

    def test_doc_reference_loads_selected_services_unchanged_shared_build_dependencies(self):
        mapping = {"services/voice/voice": "org/voice", "libs/nest-shared": "org/nest-shared",
                   "libs/unrelated": "org/unrelated"}
        group = self.snapshots["head"] / "services/voice"
        group.mkdir(parents=True)
        (group / "compose.yml").write_text("services:\n  voice:\n    build:\n"
                                           "      additional_contexts:\n"
                                           "        shared: ../../libs/nest-shared\n")
        selected, reasons, _ = self.select(mapping, mapping, [{"filename": "docs/handoff.md"}], "org/voice")
        self.assertEqual(set(selected["head"]), {"services/voice/voice", "libs/nest-shared"})
        self.assertIn("build context libs/nest-shared in services/voice/compose.yml", reasons["org/nest-shared"])

    def test_head_only_unknown_or_changed_url_never_grants_access(self):
        base = {"services/ai/engine": "org/engine"}
        head = {"services/ai/engine": "attacker/engine", "services/ai/new": "attacker/new"}
        selected, _, skipped = self.select(base, head, [{"filename": "services/ai/compose.yml"}], "attacker/new")
        self.assertEqual(selected, {"base": base, "head": {}})
        self.assertEqual(len(skipped), 2)

    def test_diff_repo_name_reference_and_trusted_identity_are_case_insensitive(self):
        selected, _, _ = self.select({"old": "Org/engine"}, {"new": "org/engine"}, [], "engine")
        self.assertEqual(selected, {"base": {"old": "Org/engine"}, "head": {"new": "Org/engine"}})

    def test_eight_repository_cap_counts_identities_across_moved_paths(self):
        base = {f"old/{i}": f"org/repo{i}" for i in range(8)}
        head = {f"services/ai/{i}": f"org/repo{i}" for i in range(8)}
        selected, _, _ = self.select(base, head, [{"filename": "services/ai/compose.yml"}])
        self.assertEqual(len(selected["head"]), 8)
        head["services/ai/ninth"] = "org/ninth"
        base["old/ninth"] = "org/ninth"
        with self.assertRaisesRegex(ValueError, "eight"):
            self.select(base, head, [{"filename": "services/ai/compose.yml"}])

    def test_unsafe_gitmodule_paths_rejected_and_urls_filtered(self):
        self.modules("base", {"../escape": "org/repo"})
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            submodule_sources(self.snapshots["base"])
        (self.snapshots["base"] / ".gitmodules").write_text(
            '[submodule "safe"]\npath = safe\nurl = https://example.com/org/repo.git\n')
        self.assertEqual(submodule_sources(self.snapshots["base"]), {})

    def expand(self, base, head, trees, fetches):
        self.modules("base", base)
        self.modules("head", head)
        calls = []

        def api(endpoint):
            calls.append(endpoint)
            return trees["head" if "/head-revision?" in endpoint else "base"]

        def gh(*args):
            fetches.append(args[-1])
            return args[-1].encode()

        def extract(data, target):
            (target / "source.txt").write_bytes(data)
            return ["symlink"]

        records = expand_referenced_submodules(self.work, "org/root", {
            "base": {"sha": "base-revision"}, "head": {"sha": "head-revision"}}, "engine", [],
            api=api, gh=gh, extract_snapshot=extract, log=logging.getLogger("test"))
        return records, calls

    def test_expansion_fetches_exact_gitlinks_and_records_reasons(self):
        base, head = {"old/engine": "org/engine"}, {"services/ai/engine": "org/engine"}
        trees = {"base": {"tree": [{"mode": "160000", "path": "old/engine", "sha": "a" * 40}]},
                 "head": {"tree": [{"mode": "160000", "path": "services/ai/engine", "sha": "b" * 40}]}}
        fetches = []
        records, calls = self.expand(base, head, trees, fetches)
        self.assertEqual(fetches, [f"repos/org/engine/tarball/{'b' * 40}", f"repos/org/engine/tarball/{'a' * 40}"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(records["head/services/ai/engine"]["sha"], "b" * 40)
        self.assertEqual(records["base/old/engine"]["omitted_links"], ["symlink"])
        self.assertEqual(records["base/old/engine"]["selection_reasons"], ["explicit diff reference"])

    def test_identical_pins_reuse_archive_at_moved_paths(self):
        trees = {side: {"tree": [{"mode": "160000", "path": path, "sha": "a" * 40}]}
                 for side, path in (("base", "old/engine"), ("head", "new/engine"))}
        fetches = []
        self.expand({"old/engine": "org/engine"}, {"new/engine": "org/engine"}, trees, fetches)
        self.assertEqual(len(fetches), 1)
        self.assertEqual((self.snapshots["base"] / "old/engine/source.txt").read_bytes(),
                         (self.snapshots["head"] / "new/engine/source.txt").read_bytes())

    def test_truncated_tree_aborts_before_archive_download(self):
        fetches = []
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.expand({"engine": "org/engine"}, {"engine": "org/engine"},
                        {"head": {"truncated": True}}, fetches)
        self.assertEqual(fetches, [])

    def test_untrusted_head_is_not_fetched_and_missing_gitlink_logged(self):
        fetches = []
        with self.assertLogs("test", level="INFO") as logs:
            records, calls = self.expand({"engine": "org/engine"}, {"engine": "attacker/engine"},
                                         {"base": {"tree": []}}, fetches)
        self.assertEqual(records, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(fetches, [])
        self.assertTrue(any("not trusted" in line for line in logs.output))
        self.assertTrue(any("no gitlink" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
