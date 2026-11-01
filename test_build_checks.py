import base64
import copy
import io
import json
import logging
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import reviewer as r
import build_checks as b


class BuildChecksTests(unittest.TestCase):
    def test_validation_failure_cannot_be_approved(self):
        clean = {"complete": True, "summary": "Change.", "limitations": "None.", "findings": []}
        for status in ("failed", "blocked", "timeout"):
            with self.subTest(status=status):
                self.assertIsNone(r.review_body({**copy.deepcopy(clean), "_validation": {
                    "enabled": True, "status": status, "jobs": []}}, "a" * 40))
        payload = r.review_body({**clean, "_validation": {"enabled": True, "status": "passed", "jobs": [
            {"name": "service", "status": "passed", "stages": [{"name": "test", "status": "passed"}]}]}}, "a" * 40)
        self.assertIn("test passed", payload["body"])
        self.assertNotIn("tests were not run", payload["body"])

    def test_partial_findings_still_request_changes_when_tests_blocked(self):
        result = {"complete": True, "summary": "Change.", "limitations": "None.",
                  "findings": [{"severity": "Must fix", "file": "x.ts", "line": 1,
                                "problem": "Broken.", "fix": "Fix.", "impact": "Failure."}],
                  "_validation": {"enabled": True, "status": "blocked", "jobs": []}}
        payload = r.review_body(result, "a" * 40)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertIn("Partial review", payload["body"])

    def test_archive_preserves_only_safe_execute_permission(self):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as archive:
            member = tarfile.TarInfo("repo/script.sh")
            member.size, member.mode = 2, 0o6755
            archive.addfile(member, io.BytesIO(b"ok"))
        with tempfile.TemporaryDirectory() as folder:
            r.extract_snapshot(content.getvalue(), Path(folder))
            self.assertEqual((Path(folder) / "script.sh").stat().st_mode & 0o7777, 0o700)

    def test_disabled_or_docs_only_never_run_docker(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(b, "run_sandbox") as sandbox:
            work = Path(folder)
            for settings in ({}, {"enabled": True}):
                report = b.run_checks(work, "owner/repo", {"files": [{"filename": "README.md"}]},
                                      settings, api=None, gh=None, extract_snapshot=None,
                                      log=logging.getLogger("test"))
                self.assertIn(report["status"], ("skipped", "not_applicable"))
            sandbox.assert_not_called()

    def test_sandbox_results_are_retained_without_modifying_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "head").mkdir()
            (work / "head/package.json").write_text(json.dumps({"name": "example", "scripts": {
                "build": "node build.js", "test": "node test.js"}}))
            original = (work / "head/package.json").read_bytes()
            result = {"status": "passed", "stages": [{"name": "test", "status": "passed", "output": "OK"}],
                      "reason": "", "cleanup_errors": []}
            with patch.object(b, "run_sandbox", return_value=result) as sandbox:
                report = b.run_checks(work, "owner/repo", {"files": [{"filename": "src/app.js"}]},
                                      {"enabled": True, "node_image": "runner:1"}, api=None, gh=None,
                                      extract_snapshot=None, log=logging.getLogger("test"))
            self.assertEqual(report["status"], "passed")
            self.assertEqual((work / "head/package.json").read_bytes(), original)
            self.assertEqual(report["jobs"][0]["stages"][0]["output_tail"], "OK")
            self.assertEqual((work / "validation-logs/0-0.log").read_text(), "OK")
            self.assertNotEqual(sandbox.call_args.args[0], work / "head")

    def test_malformed_source_becomes_setup_blocker(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            (work / "head").mkdir()
            (work / "head/package.json").write_text("invalid")
            report = b.run_checks(work, "owner/repo", {"files": [{"filename": "package.json"}]},
                                  {"enabled": True}, api=None, gh=None, extract_snapshot=None,
                                  log=logging.getLogger("test"))
            self.assertEqual(report["status"], "blocked")

    def test_validation_configuration_invalidates_saved_deferral(self):
        pr = {"head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
        with patch.object(r, "CONFIG", {}):
            previous = r.deferred_revision(pr)
        with patch.object(r, "CONFIG", {"validation": {"enabled": True}}):
            self.assertNotEqual(previous, r.deferred_revision(pr))

    def test_transient_setup_deferral_retries_after_delay(self):
        pr = {"state": "open", "draft": False, "user": {"login": "author"},
              "requested_reviewers": [{"login": "reviewer"}],
              "head": {"sha": "a" * 40}, "base": {"sha": "b" * 40}}
        result = {"complete": True, "summary": "Change.", "limitations": "None.", "findings": [],
                  "_validation": {"enabled": True, "status": "blocked", "jobs": []}}
        state = {}
        with patch.object(r, "CONFIG", {"user": "reviewer", "repositories": ["owner/repo"]}), \
                patch.object(r, "api", return_value=pr), patch.object(r, "paginated", return_value=[]), \
                patch.object(r, "save_state"), patch.object(r, "evaluate", side_effect=lambda *a: copy.deepcopy(result)) as evaluate:
            with patch.object(r.time, "time", return_value=10000):
                r.review_one("owner/repo", 1, state)
                r.review_one("owner/repo", 1, state)
                self.assertEqual(evaluate.call_count, 1)
                self.assertEqual(state["owner/repo#1"]["deferred_retry_at"], 13600)
            with patch.object(r.time, "time", return_value=13601):
                r.review_one("owner/repo", 1, state)
                self.assertEqual(evaluate.call_count, 2)


class WorkspaceDependencyRevisionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.head = Path(folder.name) / 'head'
        self.head.mkdir()
        (self.head / 'package.json').write_text(json.dumps({
            'name': 'service', 'dependencies': {'nest-shared': 'workspace:*'}}))
        self.pin = '8b8a7938e04452b5b0b81e9204f61eb7ab933ff5'
        self.rule = {'parent_repository': 'owner/backend', 'repository': 'owner/nest-shared'}

    def fetch(self, api, rule=None):
        def extract(data, destination):
            (destination / 'package.json').write_text(json.dumps({'name': 'nest-shared'}))
        gh = unittest.mock.Mock(return_value=b'archive')
        records = b.add_workspace_dependencies(
            self.head, {'workspace_dependencies': {'nest-shared': rule or self.rule}},
            api=api, gh=gh, extract_snapshot=extract, log=logging.getLogger('test'))
        return records, gh

    def test_explicit_revision_uses_ci_commit_without_parent_lookup(self):
        api = unittest.mock.Mock(side_effect=AssertionError('Unexpected parent lookup'))
        records, gh = self.fetch(api, {**self.rule, 'revision': self.pin})
        api.assert_not_called()
        gh.assert_called_once_with('api', '--hostname', 'github.com',
                                   f'repos/owner/nest-shared/tarball/{self.pin}')
        self.assertEqual(records[0]['sha'], self.pin)
        self.assertIsNone(records[0]['parent_sha'])
        self.assertIn('configured revision', records[0]['note'])
        self.assertIn('not a pin declared by this standalone PR', records[0]['note'])

    def test_missing_revision_preserves_approved_parent_gitlink(self):
        parent_sha = 'a' * 40
        metadata = base64.b64encode(b'[submodule "shared"]\npath = libs/shared\nurl = git@github.com:owner/nest-shared.git\n').decode()
        responses = {
            'repos/owner/backend': {'default_branch': 'main'},
            'repos/owner/backend/commits/main': {'sha': parent_sha},
            f'repos/owner/backend/contents/.gitmodules?ref={parent_sha}': {'content': metadata},
            f'repos/owner/backend/git/trees/{parent_sha}?recursive=1': {
                'tree': [{'path': 'libs/shared', 'mode': '160000', 'sha': self.pin}]},
        }
        api = unittest.mock.Mock(side_effect=lambda endpoint: responses[endpoint])
        records, gh = self.fetch(api)
        self.assertEqual(records[0]['parent_sha'], parent_sha)
        self.assertEqual(records[0]['sha'], self.pin)
        self.assertIn('approved parent', records[0]['note'])

    def test_invalid_explicit_revision_fails_before_fetching_source(self):
        for value in ('develop', '', None, '../main', 'b' * 39, 'b' * 41):
            with self.subTest(revision=value):
                api = unittest.mock.Mock(side_effect=AssertionError('Unexpected lookup'))
                with self.assertRaisesRegex(ValueError, 'Invalid validation dependency pin'):
                    self.fetch(api, {**self.rule, 'revision': value})
                api.assert_not_called()

    def test_existing_workspace_dependency_keeps_its_snapshot(self):
        shared = self.head / 'libs/shared'
        shared.mkdir(parents=True)
        manifest = json.dumps({'name': 'nest-shared'})
        (shared / 'package.json').write_text(manifest)
        api = unittest.mock.Mock(side_effect=AssertionError('Unexpected lookup'))
        records, gh = self.fetch(api, {**self.rule, 'revision': self.pin})
        self.assertEqual(records, [])
        self.assertEqual((shared / 'package.json').read_text(), manifest)
        gh.assert_not_called()
        api.assert_not_called()

    def test_revision_change_invalidates_saved_deferral(self):
        pr = {'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}
        config = {'validation': {'workspace_dependencies': {'nest-shared': self.rule}}}
        with patch.object(r, 'CONFIG', config):
            before = r.deferred_revision(pr)
            self.rule['revision'] = self.pin
            self.assertNotEqual(before, r.deferred_revision(pr))


class WorkspaceDependencyBranchTests(unittest.TestCase):
    setUp = WorkspaceDependencyRevisionTests.setUp
    fetch = WorkspaceDependencyRevisionTests.fetch

    def test_branch_is_resolved_fresh_and_downloaded_by_exact_sha(self):
        rule = {**self.rule, 'branch': 'develop'}
        next_sha = 'c' * 40
        api = unittest.mock.Mock(side_effect=[{'sha': self.pin}, {'sha': next_sha}])
        first, gh = self.fetch(api, rule)
        self.assertEqual(first[0]['sha'], self.pin)
        self.assertEqual(first[0]['branch'], 'develop')
        gh.assert_called_once_with('api', '--hostname', 'github.com',
                                   f'repos/owner/nest-shared/tarball/{self.pin}')
        self.assertIn('configured branch develop', first[0]['note'])
        self.head = self.head.parent / 'second-head'
        self.head.mkdir()
        (self.head / 'package.json').write_text(json.dumps({
            'name': 'service', 'dependencies': {'nest-shared': 'workspace:*'}}))
        second, gh = self.fetch(api, rule)
        self.assertEqual(second[0]['sha'], next_sha)
        self.assertEqual(api.call_args_list, [
            unittest.mock.call('repos/owner/nest-shared/commits/develop'),
            unittest.mock.call('repos/owner/nest-shared/commits/develop')])
        gh.assert_called_once_with('api', '--hostname', 'github.com',
                                   f'repos/owner/nest-shared/tarball/{next_sha}')

    def test_explicit_snapshot_is_not_replaced_by_configured_branch(self):
        shared = self.head / 'shared'
        shared.mkdir()
        (shared / 'package.json').write_text(json.dumps({'name': 'nest-shared'}))
        api = unittest.mock.Mock(side_effect=AssertionError('Unexpected lookup'))
        records, gh = self.fetch(api, {**self.rule, 'branch': 'develop'})
        self.assertEqual(records, [])
        api.assert_not_called()
        gh.assert_not_called()

    def test_branch_cannot_be_combined_with_revision(self):
        api = unittest.mock.Mock(side_effect=AssertionError('Unexpected lookup'))
        with self.assertRaisesRegex(ValueError, 'either revision or branch'):
            self.fetch(api, {**self.rule, 'revision': self.pin, 'branch': 'develop'})
        api.assert_not_called()

    def test_bad_branch_or_resolution_fails_closed(self):
        for branch in ('', None, '../develop', 'develop?ref=other'):
            with self.subTest(branch=branch):
                api = unittest.mock.Mock(side_effect=AssertionError('Unexpected lookup'))
                with self.assertRaisesRegex(ValueError, 'Invalid validation dependency branch'):
                    self.fetch(api, {**self.rule, 'branch': branch})
                api.assert_not_called()
        for sha in (None, 'develop', 'a' * 39):
            with self.subTest(sha=sha):
                with self.assertRaisesRegex(ValueError, 'Invalid validation dependency pin'):
                    self.fetch(unittest.mock.Mock(return_value={'sha': sha}),
                               {**self.rule, 'branch': 'develop'})

    def test_deferred_review_retries_only_when_used_branch_changes(self):
        pr = {'state': 'open', 'draft': False, 'user': {'login': 'author'},
              'requested_reviewers': [{'login': 'reviewer'}],
              'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}
        dependency = {'repository': 'owner/nest-shared', 'branch': 'develop', 'sha': self.pin}
        result = {'complete': True, 'summary': 'Change.', 'limitations': 'Build failed.',
                  'findings': [], '_validation': {'enabled': True, 'status': 'failed',
                  'jobs': [], 'dependencies': [dependency]}}
        config = {'user': 'reviewer', 'repositories': ['owner/repo'],
                  'validation': {'enabled': True, 'workspace_dependencies': {
                      'nest-shared': {**self.rule, 'branch': 'develop'}}}}
        current_sha = self.pin
        def api(endpoint):
            if endpoint == 'repos/owner/repo/pulls/1': return pr
            if endpoint == 'repos/owner/nest-shared/commits/develop': return {'sha': current_sha}
            raise AssertionError(endpoint)
        state = {}
        with patch.object(r, 'CONFIG', config), patch.object(r, 'api', side_effect=api), \
                patch.object(r, 'paginated', return_value=[]), patch.object(r, 'save_state'), \
                patch.object(r, 'evaluate', side_effect=lambda *args: copy.deepcopy(result)) as evaluate:
            r.review_one('owner/repo', 1, state)
            self.assertEqual(state['owner/repo#1']['deferred_revision'][4],
                             [['owner/nest-shared', 'develop', self.pin]])
            r.review_one('owner/repo', 1, state)
            self.assertEqual(evaluate.call_count, 1)
            current_sha = 'd' * 40
            dependency['sha'] = current_sha
            r.review_one('owner/repo', 1, state)
            self.assertEqual(evaluate.call_count, 2)
            r.review_one('owner/repo', 1, state)
            self.assertEqual(evaluate.call_count, 2)
            self.assertEqual(state['owner/repo#1']['deferred_revision'][4][0][2], current_sha)

    def test_completed_review_does_not_resolve_branch_or_review_again(self):
        pr = {'state': 'open', 'draft': False, 'user': {'login': 'author'},
              'requested_reviewers': [{'login': 'reviewer'}],
              'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}
        config = {'user': 'reviewer', 'repositories': ['owner/repo'], 'validation': {
                  'workspace_dependencies': {'nest-shared': {**self.rule, 'branch': 'develop'}}}}
        with patch.object(r, 'CONFIG', config), patch.object(r, 'api', return_value=pr) as api, \
                patch.object(r, 'paginated', return_value=[{'user': {'login': 'reviewer'},
                    'commit_id': 'a' * 40, 'state': 'APPROVED'}]), patch.object(r, 'evaluate') as evaluate:
            r.review_one('owner/repo', 1, {})
            api.assert_called_once_with('repos/owner/repo/pulls/1')
            evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
