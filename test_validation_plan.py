import json
from pathlib import Path
import tempfile
import unittest

from validation_plan import discover_targets, plan_checks, prepare_target


class ValidationPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.head = self.work / "head"
        self.head.mkdir()
        self.addCleanup(self.temporary.cleanup)

    def node(self, path=".", **extra):
        target = self.head / path
        target.mkdir(parents=True, exist_ok=True)
        value = {"name": "service", "version": "1.0.0", "scripts": {"build": "tsc", "test": "jest"}}
        value.update(extra)
        (target / "package.json").write_text(json.dumps(value))
        return target

    def plan(self, path="."):
        return prepare_target(self.work, path, self.work / "inputs", {"node_image": "node:test"})

    def python(self, extra=""):
        (self.head / "pyproject.toml").write_text(
            '[project]\nname="example"\nversion="1.0.0"\nrequires-python=">=3.12"\n'
            'dependencies=["requests>=2"]\n[project.optional-dependencies]\ndev=["pytest"]\n'
            '[build-system]\nrequires=["hatchling"]\nbuild-backend="hatchling.build"\n' + extra)

    def test_docs_only_has_no_targets(self):
        self.node()
        self.assertEqual(discover_targets(self.work, "owner/service", {"files": [{"filename": "README.md"}]}), [])

    def test_standalone_source_is_selected(self):
        self.node()
        self.assertEqual(discover_targets(self.work, "owner/service", {"files": [{"filename": "src/main.ts"}]}), ["."])

    def test_runtime_text_files_trigger_checks(self):
        self.node()
        for name in ("requirements.txt", "prompts/runtime.txt", "fixtures/input.txt"):
            self.assertEqual(discover_targets(self.work, "owner/service", {"files": [{"filename": name}]}), ["."])
        self.assertEqual(discover_targets(self.work, "owner/service", {"files": [{"filename": "docs/notes.txt"}]}), [])

    def test_group_compose_selects_affected_submodules_not_companions(self):
        self.node("services/voice/voice")
        self.node("services/voice/bridge", name="bridge")
        self.node("services/unrelated", name="other")
        context = {"files": [{"filename": "services/voice/compose.yml"}], "expanded_submodules": {
            "head/services/voice/voice": {"selection_reasons": ["service group services/voice/compose.yml"]},
            "head/services/voice/bridge": {"selection_reasons": ["build context services/voice/bridge in compose"]},
            "head/services/unrelated": {"selection_reasons": ["explicit diff reference"]}}}
        self.assertEqual(discover_targets(self.work, "owner/parent", context),
                         ["services/voice/bridge", "services/voice/voice"])

    def test_shared_build_context_is_only_dependency_unless_changed(self):
        self.node("libs/shared", name="nest-shared", scripts={"build": "tsc"})
        context = {"files": [{"filename": "services/voice/compose.yml"}], "expanded_submodules": {
            "head/libs/shared": {"selection_reasons": ["build context libs/shared in compose"]}}}
        self.assertEqual(discover_targets(self.work, "owner/parent", context), [])
        context["files"] = [{"filename": "libs/shared"}]
        self.assertEqual(discover_targets(self.work, "owner/parent", context), ["libs/shared"])

    def test_installer_sees_only_sanitized_metadata(self):
        self.node(dependencies={"typescript": "^5.0.0"}, scripts={"postinstall": "touch owned", "test": "jest"},
                  pnpm={"patchedDependencies": {"typescript": "owned.patch"}}, packageManager="npm@1")
        (self.head / ".npmrc").write_text("registry=https://evil.invalid")
        (self.head / "owned.js").write_text("throw 1")
        result = self.plan()
        files = sorted(str(path.relative_to(result["dependency_input"]))
                       for path in result["dependency_input"].rglob("*") if path.is_file())
        self.assertEqual(files, ["package.json", "pnpm-workspace.yaml"])
        manifest = json.loads((result["dependency_input"] / "package.json").read_text())
        self.assertNotIn("scripts", manifest)
        self.assertNotIn("pnpm", manifest)
        self.assertIn("--ignore-scripts", result["stages"][0]["argv"])
        self.assertIn("--ignore-pnpmfile", result["stages"][0]["argv"])
        self.assertTrue(result["stages"][0]["network"])
        self.assertFalse(result["stages"][-1]["network"])

    def test_only_matching_standalone_lock_is_frozen(self):
        self.node()
        (self.head / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        self.assertEqual(self.plan()["dependency_resolution"], "locked")

    def test_workspace_dependency_uses_snapshot_and_compiles_shared_first(self):
        self.node("services/voice", dependencies={"nest-shared": "workspace:*"})
        self.node("libs/shared", name="nest-shared", scripts={"build": "pnpm install && rm -rf node_modules"})
        result = self.plan("services/voice")
        self.assertEqual(result["local_dependencies"], ["libs/shared"])
        self.assertEqual(result["stages"][1]["argv"], ["pnpm", "exec", "tsc", "-p", "tsconfig.build.json"])
        self.assertEqual(result["stages"][1]["cwd"], "/workspace/libs/shared")
        self.assertIn("unlocked", result["dependency_resolution"])

    def test_missing_shared_snapshot_blocks_instead_of_using_host(self):
        self.node(dependencies={"nest-shared": "workspace:*"})
        result = self.plan()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("nest-shared", result["reason"])
        self.assertEqual(result["stages"], [])

    def test_remote_and_escaping_local_dependencies_block(self):
        for spec in ("git+https://evil.invalid/repo", "https://evil.invalid/a.tgz", "file:../../outside", "link:../../outside"):
            self.node(dependencies={"dep": spec})
            self.assertEqual(self.plan()["status"], "blocked", spec)

    def test_invalid_package_name_blocks_before_install(self):
        self.node(name="--arbitrary-option")
        self.assertEqual(self.plan()["status"], "blocked")

    def test_install_only_package_cannot_report_checks_passed(self):
        self.node(scripts={"prepare": "husky"})
        result = self.plan()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stages"], [])
        self.assertIn("build or test", result["reason"])

    def test_bridge_prefers_existing_make_targets(self):
        self.node(name="notify-voice-ai-bridge", scripts={"build": "nest build", "test": "nest build && node --test test/*.test.js"})
        (self.head / "Makefile").write_text("build:\n\tpnpm build\n")
        result = self.plan()
        self.assertEqual(result["stages"][-1]["argv"], ["make", "test", "PNPM=pnpm"])

    def test_notify_parent_prefers_existing_make_service_alias(self):
        self.node(name="notify-backend", scripts={})
        self.node("services/auth/auth", name="notify-auth-service")
        (self.head / "Makefile").write_text("build:\n\tpnpm build\n")
        result = self.plan("services/auth/auth")
        self.assertEqual(result["stages"][1]["argv"], ["make", "build", "s=auth"])
        self.assertEqual(result["stages"][1]["cwd"], "/workspace")
        self.assertEqual(result["stages"][-1]["argv"], ["pnpm", "run", "test", "--runInBand"])
        self.assertEqual(result["stages"][-1]["cwd"], "/workspace/services/auth/auth")

    def test_python_source_is_offline_and_deps_binary_only(self):
        self.python()
        result = self.plan()
        self.assertEqual(result["kind"], "python")
        self.assertIn("unlocked", result["dependency_resolution"])
        binary = next(stage for stage in result["stages"] if stage["name"] == "install binary build dependencies")
        self.assertIn("--only-binary", binary["argv"])
        self.assertTrue(binary["network"])
        self.assertFalse(any(stage["network"] for stage in result["stages"] if stage["name"] in ("build", "install project", "test")))
        self.assertIn("--offline", result["stages"][-2]["argv"])

    def test_python_frozen_sync_never_installs_or_builds_project_on_network(self):
        self.python()
        (self.head / "uv.lock").write_text('version=1\n[[package]]\nname="example"\nsource={editable="."}\n')
        result = self.plan()
        self.assertEqual(result["dependency_resolution"], "locked")
        self.assertIn("--no-install-project", result["stages"][0]["argv"])
        self.assertIn("--no-build", result["stages"][0]["argv"])
        self.assertNotIn("build-backend", (result["dependency_input"] / "pyproject.toml").read_text())

    def test_python_remote_lock_source_blocks(self):
        self.python()
        (self.head / "uv.lock").write_text('version=1\n[[package]]\nname="bad"\nsource={git="https://evil.invalid"}\n')
        self.assertEqual(self.plan()["status"], "blocked")

    def test_python_discards_download_cache_before_packaging_for_locked_and_unlocked_deps(self):
        self.python()
        for frozen in (False, True):
            with self.subTest(frozen=frozen):
                if frozen:
                    (self.head / "uv.lock").write_text(
                        'version=1\n[[package]]\nname="example"\nsource={editable="."}\n')
                stages = self.plan()["stages"]
                cleanup = next(index for index, stage in enumerate(stages)
                               if stage["name"] == "clear dependency cache")
                self.assertTrue(all(stage["network"] for stage in stages[:cleanup]))
                self.assertFalse(any(stage["network"] for stage in stages[cleanup:]))
                self.assertEqual(stages[cleanup]["argv"], ["uv", "cache", "clean"])
                self.assertEqual(stages[cleanup + 1]["name"], "build")
                self.assertEqual(stages[-1]["name"], "test")

    def test_target_limit_is_explicit(self):
        context = {"files": [{"filename": "compose.yml"}], "expanded_submodules": {}}
        for index in range(3):
            path = "service" + str(index)
            self.node(path, name=path)
            context["expanded_submodules"]["head/" + path] = {"selection_reasons": ["build context " + path]}
        plans = plan_checks(self.work, "owner/parent", context, {"max_targets": 2})
        self.assertEqual(len(plans), 3)
        self.assertEqual(plans[-1]["status"], "blocked")
        self.assertIn("service2", plans[-1]["reason"])


if __name__ == "__main__":
    unittest.main()
