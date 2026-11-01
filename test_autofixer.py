import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

import autofixer as a


class AutofixerTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.object(a.r, 'CONFIG', {'user': 'Johnndelembi', 'repositories': ['owner/repo'],
                                                 'gh': '/opt/homebrew/bin/gh', 'validation': {'enabled': True},
                                                 'autofix': {'commit_email': '1+Johnndelembi@users.noreply.github.com'}})
        self.config.start()
        self.addCleanup(self.config.stop)
        self.pr = {'state': 'open', 'draft': False, 'user': {'login': 'Johnndelembi'},
                   'head': {'sha': 'a' * 40, 'ref': 'feature/x', 'repo': {'full_name': 'owner/repo'}},
                   'base': {'sha': 'b' * 40}}
        self.item = {'key': 'owner/repo#1:issue:42:abc', 'kind': 'issue', 'id': 42,
                     'body': '[REQUEST CHANGE] Fix retry handling.\nKeep tests.',
                     'url': 'https://github.com/owner/repo/issues/1#issuecomment-42', 'author': 'reviewer'}

    def test_only_own_open_non_draft_same_repository_prs(self):
        self.assertTrue(a.own_pr('owner/repo', self.pr))
        for field, value in [('state', 'closed'), ('draft', True), ('user', {'login': 'other'}),
                             ('head', {'repo': None}), ('head', {'repo': {'full_name': 'fork/repo'}})]:
            pr = {**self.pr, field: value}
            self.assertFalse(a.own_pr('owner/repo', pr))
        self.assertFalse(a.own_pr('other/repo', self.pr))

    def test_schedule_switches_at_eleven_and_seventeen_and_survives_restarts(self):
        cases = [(10, 59, True), (11, 0, True), (11, 10, False), (11, 20, False),
                 (11, 30, True), (11, 50, False), (16, 59, True), (17, 0, True),
                 (17, 5, False), (17, 10, True), (17, 20, True), (23, 59, True)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {**a.r.CONFIG, 'autofix': {'enabled': True}}
            (root / 'config.json').write_text(json.dumps(config))
            reviewer_schedule = root / 'poll-schedule.json'
            reviewer_schedule.write_text('reviewer-owned state')
            with patch.object(a, 'ROOT', root), patch.object(a.r, 'ROOT', root), \
                    patch.object(sys, 'argv', ['autofixer.py']), \
                    patch.object(a, 'RotatingFileHandler'), patch.object(a.LOG, 'addHandler'), \
                    patch.object(a.r, 'api', return_value={'login': 'Johnndelembi'}) as api, \
                    patch.object(a.r, 'gh', return_value=b'[]') as github:
                for hour, minute, expected in cases:
                    now = datetime(2026, 10, 30, hour, minute, tzinfo=a.r.REVIEW_TIMEZONE)
                    window = a.r.poll_window(now.astimezone(timezone.utc))
                    api.reset_mock()
                    github.reset_mock()
                    with self.subTest(hour=hour, minute=minute), \
                            patch.object(a.r, 'poll_window', return_value=window):
                        a.main()
                        self.assertEqual(api.call_count, int(expected))
                        self.assertEqual(github.call_count, int(expected))
                self.assertEqual(reviewer_schedule.read_text(), 'reviewer-owned state')
                saved = json.loads((root / 'autofix-poll-schedule.json').read_text())
                self.assertEqual(saved['last_window'], window[0].isoformat())

    def test_manual_check_and_dry_run_bypass_the_poll_schedule(self):
        for flag in ('--check', '--dry-run'):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config.json').write_text(json.dumps(a.r.CONFIG))
                with patch.object(a, 'ROOT', root), patch.object(sys, 'argv', ['autofixer.py', flag]), \
                        patch.object(a, 'RotatingFileHandler'), patch.object(a.LOG, 'addHandler'), \
                        patch.object(a.r, 'claim_poll_window') as claim, \
                        patch.object(a.r, 'api', return_value={'login': 'Johnndelembi'}), \
                        patch.object(a.r, 'gh', return_value=b'[]'):
                    a.main()
                    claim.assert_not_called()
                self.assertFalse((root / 'autofix-poll-schedule.json').exists())

    def test_regular_feedback_from_trusted_authors_is_discovered_without_reply_loops(self):
        def comment(id, body, author='reviewer', association='MEMBER', state='COMMENTED'):
            return {'id': id, 'body': body, 'user': {'login': author}, 'author_association': association,
                    'html_url': f'https://github.com/c/{id}', 'created_at': '2026-10-27', 'state': state}
        data = [comment(1, '[REQUEST CHANGE] fix'), comment(2, 'Please fix'),
                comment(3, '[request change] fix'), comment(4, '[REQUEST CHANGE] fix', association='NONE'),
                comment(5, '[REQUEST CHANGE] own', author='Johnndelembi', association='NONE'),
                comment(6, '[REQUEST CHANGE] quote\n' + a.MARKER + 'abc -->'),
                comment(7, '   ')]
        with patch.object(a.r, 'paginated', return_value=data):
            found = a.requests('owner/repo', 1)
        self.assertEqual(len(found), 12)  # IDs 1, 2, 3, and 5 from each surface.
        self.assertEqual({item['kind'] for item in found}, {'issue', 'inline', 'review'})
        self.assertEqual({item['id'] for item in found}, {1, 2, 3, 5})

    def test_dismissed_reviews_are_not_requests(self):
        comment = {'id': 1, 'body': '[REQUEST CHANGE] fix', 'state': 'DISMISSED',
                   'user': {'login': 'Johnndelembi'}, 'html_url': 'url'}
        with patch.object(a.r, 'paginated', side_effect=[[], [], [comment]]):
            self.assertEqual(a.requests('owner/repo', 1), [])

    def test_regular_submitted_reviews_are_candidates_regardless_of_review_decision(self):
        for state in ('COMMENTED', 'APPROVED', 'CHANGES_REQUESTED', 'PENDING', 'DISMISSED'):
            comment = {'id': 1, 'body': 'The date filter accepts an impossible day.', 'state': state,
                       'user': {'login': 'reviewer'}, 'author_association': 'COLLABORATOR', 'html_url': 'url'}
            with self.subTest(state=state), patch.object(a.r, 'paginated', side_effect=[[], [], [comment]]):
                found = a.requests('owner/repo', 1)
            self.assertEqual(bool(found), state not in ('PENDING', 'DISMISSED'))

    def test_inline_reply_keeps_source_and_thread_location_without_a_trigger(self):
        comment = {'id': 2, 'body': 'This still allows invalid dates.', 'in_reply_to_id': 1,
                   'path': 'src/dto.ts', 'line': 49, 'commit_id': 'a' * 40,
                   'user': {'login': 'reviewer'}, 'author_association': 'MEMBER', 'html_url': 'url'}
        with patch.object(a.r, 'paginated', side_effect=[[], [comment], []]):
            found = a.requests('owner/repo', 1)
        self.assertEqual(len(found), 1)
        for name in ('in_reply_to_id', 'path', 'line', 'commit_id', 'body'):
            self.assertEqual(found[0][name], comment[name])

    def test_regular_feedback_from_outside_trusted_authors_remains_ignored(self):
        comment = {'id': 1, 'body': 'Please change the tenant check.',
                   'user': {'login': 'outsider'}, 'author_association': 'NONE', 'html_url': 'url'}
        with patch.object(a.r, 'paginated', side_effect=[[comment], [], []]):
            self.assertEqual(a.requests('owner/repo', 1), [])

    def test_editing_request_changes_idempotency_key(self):
        comment = {'id': 1, 'body': '[REQUEST CHANGE] fix', 'state': 'COMMENTED',
                   'user': {'login': 'Johnndelembi'}, 'html_url': 'url'}
        with patch.object(a.r, 'paginated', side_effect=[[comment], [], []]):
            first = a.requests('owner/repo', 1)[0]
        comment['body'] += ' more'
        with patch.object(a.r, 'paginated', side_effect=[[comment], [], []]):
            second = a.requests('owner/repo', 1)[0]
        self.assertNotEqual(first['key'], second['key'])

    def test_comment_quotes_original_and_includes_commit_and_validation(self):
        record = {'summary': 'Fixed retry handling.', 'evidence': 'Changed worker.py.',
                  'validation': 'Build/test validation: passed.', 'limitations': 'None identified', 'commit': 'c' * 40}
        body = a.reply_body(self.item, record)
        self.assertIn('> [REQUEST CHANGE] Fix retry handling.\n> Keep tests.', body)
        for value in (record['summary'], record['commit'], record['validation'], self.item['url'], a.marker(self.item)):
            self.assertIn(value, body)

    def test_changed_head_base_branch_or_deleted_request_blocks_publish(self):
        for side in ('head', 'base'):
            changed = copy.deepcopy(self.pr)
            changed[side]['sha'] = 'd' * 40
            with patch.object(a.r, 'api', side_effect=[{'login': 'Johnndelembi'}, changed]):
                with self.assertRaisesRegex(ValueError, 'PR changed'):
                    a.ensure_fresh('owner/repo', 1, self.pr, self.item)
        changed = copy.deepcopy(self.pr)
        changed['head']['ref'] = 'different'
        with patch.object(a.r, 'api', side_effect=[{'login': 'Johnndelembi'}, changed]):
            with self.assertRaises(ValueError):
                a.ensure_fresh('owner/repo', 1, self.pr, self.item)
        with patch.object(a.r, 'api', side_effect=[{'login': 'Johnndelembi'}, self.pr]), \
                patch.object(a, 'requests', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'deleted, edited'):
                a.ensure_fresh('owner/repo', 1, self.pr, self.item)

    def test_account_switch_blocks_publish(self):
        with patch.object(a.r, 'api', return_value={'login': 'other'}):
            with self.assertRaisesRegex(ValueError, 'account changed'):
                a.ensure_fresh('owner/repo', 1, self.pr, self.item)

    def test_changes_detect_deletes_modes_and_reject_submodules_and_metadata(self):
        self.assertEqual(a.changed_paths({'a': ('1', False)}, {'b': ('2', False)}, {}), ['a', 'b'])
        self.assertEqual(a.changed_paths({'a': ('1', False)}, {'a': ('1', True)}, {}), ['a'])
        for path in ('services/sub/file.py', '.git/config', '.codex/config.toml'):
            with self.assertRaises(ValueError):
                a.changed_paths({}, {path: ('1', False)}, {'expanded_submodules': {'head/services/sub': {}}})
        with self.assertRaises(ValueError):
            a.changed_paths({}, {str(i): ('1', False) for i in range(31)}, {})

    def test_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                a.files_snapshot(root)

    def test_reply_retry_checks_commit_ancestry_and_deduplicates(self):
        record = {'status': 'pending_reply', 'commit': 'c' * 40}
        state = {self.item['key']: record}
        with patch.object(a.r, 'api', side_effect=[self.pr, {'status': 'ahead'}]) as api, \
                patch.object(a.r, 'paginated', return_value=[{'body': a.marker(self.item),
                                                             'user': {'login': 'Johnndelembi'}}]), \
                patch.object(a, 'save') as save:
            a.finish_reply('owner/repo', 1, self.item, record, state)
            self.assertEqual(record['status'], 'done')
            self.assertEqual(api.call_count, 2)
            save.assert_called_once()

    def test_forged_reply_marker_does_not_suppress_own_reply(self):
        record = {'status': 'pending_reply', 'summary': 'done', 'evidence': 'file.py',
                  'validation': 'passed', 'limitations': 'none'}
        with patch.object(a.r, 'api', return_value={'login': 'Johnndelembi'}) as api, \
                patch.object(a.r, 'paginated', return_value=[{'body': a.marker(self.item), 'user': {'login': 'other'}}]), \
                patch.object(a, 'save'):
            a.finish_reply('owner/repo', 1, self.item, record, {})
            self.assertEqual(api.call_count, 2)
            self.assertIn('payload', api.call_args.kwargs)

    def test_unpushed_or_rebased_commit_never_claims_fixed(self):
        record = {'status': 'pending_reply', 'commit': 'c' * 40}
        with patch.object(a.r, 'api', side_effect=[self.pr, {'status': 'diverged'}]) as api, \
                patch.object(a.r, 'paginated') as comments, patch.object(a, 'save') as save:
            with self.assertRaisesRegex(ValueError, 'manual recovery'):
                a.finish_reply('owner/repo', 1, self.item, record, {})
            comments.assert_not_called()
            save.assert_not_called()

    def setup_fix(self, root, result=None, changed=True):
        (root / 'autofix-work').mkdir()
        def prepare(work, *args):
            (work / 'head').mkdir()
            (work / 'head' / 'a.py').write_text('old')
            return {'files': [{'filename': 'a.py'}]}
        def edit(work, item):
            if changed:
                (work / 'head' / 'a.py').write_text('new')
            return result or {'status': 'fixed', 'summary': 'fixed', 'evidence': 'a.py', 'limitations': 'none',
                              'commits': [{'message': 'fix(worker): correct retry handling', 'files': ['a.py']}]}
        return prepare, edit

    def test_failed_validation_blocks_push_reply_and_pending_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare, edit = self.setup_fix(root)
            with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                    patch.object(a, 'edit', side_effect=edit), patch.object(a, 'git') as git, \
                    patch.object(a.r, 'paginated', return_value=[]), patch.object(a, 'finish_reply') as reply, \
                    patch.object(a, 'run_checks', return_value={'enabled': True, 'status': 'failed', 'jobs': []}):
                state = {}
                with self.assertRaisesRegex(ValueError, 'not published'):
                    a.fix_one('owner/repo', 1, self.pr, self.item, state)
                git.assert_not_called()
                reply.assert_not_called()
                self.assertEqual(state, {})

    def test_dry_run_validates_without_git_publish_or_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare, edit = self.setup_fix(root)
            with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                    patch.object(a, 'edit', side_effect=edit), patch.object(a, 'git') as git, \
                    patch.object(a.r, 'paginated', return_value=[]), patch.object(a, 'finish_reply') as reply, \
                    patch.object(a, 'run_checks', return_value={'enabled': True, 'status': 'passed', 'jobs': []}):
                state = {}
                a.fix_one('owner/repo', 1, self.pr, self.item, state, dry_run=True)
                git.assert_not_called()
                reply.assert_not_called()
                self.assertEqual(state, {})

    def test_non_actionable_feedback_is_recorded_without_validation_push_or_reply(self):
        result = {'status': 'not_actionable', 'summary': 'Acknowledgment only.',
                  'evidence': 'The comment says thanks and suggests no change.',
                  'limitations': 'None identified', 'commits': []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare, edit = self.setup_fix(root, result, changed=False)
            with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                    patch.object(a, 'edit', side_effect=edit), patch.object(a, 'git') as git, \
                    patch.object(a.r, 'paginated', return_value=[]), patch.object(a, 'finish_reply') as reply, \
                    patch.object(a, 'run_checks') as validation:
                state = {}
                a.fix_one('owner/repo', 1, self.pr, {**self.item, 'body': 'Thanks!'}, state)
                self.assertEqual(state[self.item['key']]['status'], 'ignored')
                self.assertEqual(state[self.item['key']]['evidence'], result['evidence'])
                saved = json.loads((root / 'autofix-state.json').read_text())
                self.assertEqual(saved, state)
                validation.assert_not_called()
                git.assert_not_called()
                reply.assert_not_called()

    def test_non_actionable_result_cannot_hide_source_edits_or_commit_plans(self):
        for changed, commits in ((True, []), (False, [{'message': 'fix: change code', 'files': ['a.py']}])):
            result = {'status': 'not_actionable', 'summary': 'No issue.', 'evidence': 'Praise only.',
                      'limitations': 'None identified', 'commits': commits}
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepare, edit = self.setup_fix(root, result, changed=changed)
                with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                        patch.object(a, 'edit', side_effect=edit), patch.object(a.r, 'paginated', return_value=[]), \
                        patch.object(a, 'run_checks') as validation:
                    state = {}
                    with self.assertRaisesRegex(ValueError, 'must not change source'):
                        a.fix_one('owner/repo', 1, self.pr, self.item, state)
                    self.assertEqual(state, {})
                    validation.assert_not_called()

    def test_non_actionable_dry_run_does_not_persist_ignored_feedback(self):
        result = {'status': 'not_actionable', 'summary': 'Status update.', 'evidence': 'No requested change.',
                  'limitations': 'None identified', 'commits': []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare, edit = self.setup_fix(root, result, changed=False)
            with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                    patch.object(a, 'edit', side_effect=edit), patch.object(a.r, 'paginated', return_value=[]), \
                    patch.object(a, 'save') as save:
                state = {}
                a.fix_one('owner/repo', 1, self.pr, self.item, state, dry_run=True)
                self.assertEqual(state, {})
                save.assert_not_called()

    def test_fix_context_includes_conversation_and_inline_parent_comments(self):
        issue = {'id': 1, 'body': 'The API should reject invalid calendar dates.'}
        parent = {'id': 2, 'body': 'Please enable strict validation.', 'path': 'src/dto.ts', 'line': 49}
        reply = {'id': 3, 'body': 'This still needs fixing.', 'in_reply_to_id': 2}
        def pages(endpoint):
            if '/files?' in endpoint:
                return [{'filename': 'src/dto.ts'}]
            return [issue] if '/issues/' in endpoint else [parent, reply]
        with tempfile.TemporaryDirectory() as directory, patch.object(a.r, 'paginated', side_effect=pages), \
                patch.object(a.r, 'gh', return_value=b'diff'), patch.object(a.r, 'extract_snapshot'), \
                patch.object(a.r, 'expand_referenced_submodules', return_value={}):
            context = a.prepare(Path(directory), 'owner/repo', 1,
                                {**self.pr, 'title': 'Date filters', 'changed_files': 1}, self.item)
            self.assertEqual(context['conversation_comments'], [issue])
            self.assertEqual(context['review_comments'], [parent, reply])

    def test_ignored_feedback_is_skipped_but_an_edited_comment_can_be_assessed(self):
        edited = {**self.item, 'key': self.item['key'] + '-edited', 'body': 'The date filter is still broken.'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {**a.r.CONFIG, 'autofix': {'enabled': True, 'max_requests_per_poll': 1}}
            (root / 'config.json').write_text(json.dumps(config))
            (root / 'autofix-state.json').write_text(json.dumps({self.item['key']: {'status': 'ignored'}}))
            found = [{'repository': {'nameWithOwner': 'owner/repo'}, 'number': 1}]
            old_handlers = list(a.LOG.handlers)
            try:
                with patch.object(a, 'ROOT', root), patch.object(sys, 'argv', ['autofixer.py']), \
                        patch.object(a.r, 'api', side_effect=lambda endpoint:
                                     {'login': 'Johnndelembi'} if endpoint == 'user' else self.pr), \
                        patch.object(a.r, 'gh', return_value=json.dumps(found).encode()), \
                        patch.object(a, 'requests', return_value=[self.item, edited]), \
                        patch.object(a, 'fix_one') as fix:
                    a.main()
                    fix.assert_called_once()
                    self.assertEqual(fix.call_args.args[3], edited)
            finally:
                for handler in list(a.LOG.handlers):
                    if handler not in old_handlers:
                        a.LOG.removeHandler(handler)
                        handler.close()

    def test_git_index_preserves_omitted_symlinks_and_unmodified_submodule(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            head = work / 'head'
            head.mkdir()
            a.git(work, 'init', '--bare', str(work / 'controller-git'))
            (head / 'a.py').write_text('old')
            a.git(work, 'add', '--', 'a.py')
            a.git(work, 'update-index', '--add', '--cacheinfo', '160000,' + 'a' * 40 + ',sub')
            a.git(work, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            original = a.git(work, 'rev-parse', 'HEAD').decode().strip()
            (head / 'sub').mkdir()
            (head / 'sub' / 'context.py').write_text('read only submodule context')
            (head / 'a.py').write_text('new')
            a.git(work, 'read-tree', original)
            a.git(work, 'add', '--', 'a.py')
            names = a.git(work, 'diff', '--cached', '--name-only').decode().splitlines()
            self.assertEqual(names, ['a.py'])


    def test_unpushed_prepared_commit_is_retried_without_false_reply(self):
        record = {'status': 'pending_reply', 'commit': 'c' * 40,
                  'original_head': self.pr['head']['sha'], 'branch': 'feature/x'}
        state = {self.item['key']: record}
        with patch.object(a.r, 'api', return_value=self.pr), patch.object(a, 'save') as save, \
                patch.object(a.r, 'paginated') as comments:
            a.finish_reply('owner/repo', 1, self.item, record, state)
            self.assertEqual(state[self.item['key']]['status'], 'retry')
            self.assertNotIn('commit', state[self.item['key']])
            save.assert_called_once()
            comments.assert_not_called()

    def test_ordinary_feedback_is_discovered_and_saved_before_compare_and_swap_push(self):
        comment = {'id': 42, 'body': 'The retry handler drops the final attempt; please correct it.',
                   'user': {'login': 'reviewer'}, 'author_association': 'COLLABORATOR',
                   'html_url': 'https://github.com/owner/repo/pull/1#issuecomment-42'}
        with patch.object(a.r, 'paginated', side_effect=[[comment], [], []]):
            requests = a.requests('owner/repo', 1)
        self.assertEqual(len(requests), 1)
        self.item = requests[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare, edit = self.setup_fix(root)
            state = {}
            calls = []
            def git(work, *args, **kwargs):
                calls.append(args)
                if args[:2] == ('rev-parse', 'FETCH_HEAD'):
                    return self.pr['head']['sha'].encode()
                if args[:2] == ('rev-parse', 'HEAD'):
                    return b'c' * 40
                if args[0] == 'diff':
                    return b'patch content'
                if args[0] == 'push':
                    self.assertEqual(state[self.item['key']]['status'], 'pending_reply')
                    self.assertEqual(state[self.item['key']]['commit'], 'c' * 40)
                    self.assertIn('--force-with-lease=refs/heads/feature/x:' + 'a' * 40, args)
                return b''
            with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                    patch.object(a, 'edit', side_effect=edit), patch.object(a, 'git', side_effect=git), \
                    patch.object(a.r, 'paginated', return_value=[]), patch.object(a, 'ensure_fresh') as fresh, \
                    patch.object(a, 'finish_reply') as reply, patch.object(a, 'save'), \
                    patch.object(a, 'run_checks', return_value={'enabled': True, 'status': 'passed', 'jobs': []}):
                a.fix_one('owner/repo', 1, self.pr, self.item, state)
                self.assertEqual(fresh.call_count, 2)
                self.assertEqual(sum(args[0] == 'push' for args in calls), 1)
                reply.assert_called_once()

    def test_unsupported_executable_validation_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepare, edit = self.setup_fix(root)
            with patch.object(a, 'ROOT', root), patch.object(a, 'prepare', side_effect=prepare), \
                    patch.object(a, 'edit', side_effect=edit), patch.object(a, 'git') as git, \
                    patch.object(a.r, 'paginated', return_value=[]), \
                    patch.object(a, 'run_checks', return_value={'enabled': True, 'status': 'not_applicable', 'jobs': []}):
                with self.assertRaisesRegex(ValueError, 'not published'):
                    a.fix_one('owner/repo', 1, self.pr, self.item, {})
                git.assert_not_called()

    def test_oversized_quote_blocks_unreportable_fix(self):
        item = {**self.item, 'body': '[REQUEST CHANGE] ' + 'x' * 60000}
        record = {'summary': 'done', 'evidence': 'a.py', 'validation': 'passed', 'limitations': 'none'}
        with self.assertRaisesRegex(ValueError, 'size limit'):
            a.reply_body(item, record)


    def test_skill_library_embeds_every_configured_source_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / 'SKILL.md'
            skill.write_text('Trusted skill instructions')
            a.r.CONFIG['autofix']['skill_files'] = [str(skill)]
            self.assertIn('SKILL SOURCE: ' + str(skill), a.skill_rules())
            self.assertIn('Trusted skill instructions', a.skill_rules())
            skill.unlink()
            with self.assertRaises(FileNotFoundError):
                a.skill_rules()
        a.r.CONFIG['autofix']['skill_files'] = []
        with self.assertRaisesRegex(ValueError, 'skill_files'):
            a.skill_rules()

    def test_commit_plan_validates_headers_group_size_and_exact_coverage(self):
        files = [f'src/{i}.py' for i in range(6)]
        valid = {'commits': [{'message': 'fix(queue): correct retry handling', 'files': files[:4]},
                             {'message': 'test(queue): cover retry boundaries', 'files': files[4:]}]}
        self.assertEqual(len(a.commit_plan(valid, files)), 2)
        for message in ('Fix: incorrect type', 'fix missing colon', 'fix: trailing period.',
                        'fix: ok\nmissing blank line', 'fix: bad\x00message'):
            with self.subTest(message=message), self.assertRaises(ValueError):
                a.commit_plan({'commits': [{'message': message, 'files': files[:1]}]}, files[:1])
        for plan in ([{'message': 'fix: all changes', 'files': files}],
                     [{'message': 'fix: some changes', 'files': files[:1]}],
                     [{'message': 'fix: wrong file', 'files': ['elsewhere.py']}],
                     [{'message': 'fix: duplicated', 'files': files[:1] * 2}]):
            with self.assertRaises(ValueError):
                a.commit_plan({'commits': plan}, files)
        self.assertEqual(a.commit_plan({'commits': []}, []), [])
        with self.assertRaises(ValueError):
            a.commit_plan({}, files)

    def test_conventional_commit_plan_preserves_breaking_change_footer(self):
        message = 'fix(api)!: require tenant scope\n\nReject missing tenants.\n\nBREAKING CHANGE: callers must send a tenant'
        plan = a.commit_plan({'commits': [{'message': message, 'files': ['api.py']}]}, ['api.py'])
        self.assertEqual(plan[0]['message'], message)


if __name__ == '__main__':
    unittest.main()
