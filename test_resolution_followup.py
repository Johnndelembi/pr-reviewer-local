import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import reviewer as r
from test_reviewer import CLEAN, PR


class ResolutionFollowupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pr = copy.deepcopy(PR)
        self.state, self.comments, self.reviews, self.posts = {}, [], [], []
        self.login = 'reviewer'
        self.fail_before_post = self.lose_response = False
        self.result = {
            **copy.deepcopy(CLEAN), 'complete': False,
            'summary': 'Rejects impossible customer-activity dates.',
            'limitations': 'The unrelated campaigns suite failed; PostgreSQL checks were skipped.',
            'prior_finding_statuses': [{
                'id': 'previous-date-finding', 'status': 'resolved',
                'evidence': 'Both date filters use strict validation; the HTTP regression passes.',
            }],
            '_validation': {'enabled': True, 'status': 'failed', 'jobs': []},
        }
        for patcher in (patch.object(r, 'ROOT', self.root),
                        patch.object(r, 'CONFIG', {'user': 'reviewer', 'repositories': ['owner/repo']}),
                        patch.object(r, 'api', side_effect=self.api),
                        patch.object(r, 'paginated', side_effect=self.paginated)):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(r, 'evaluate', side_effect=lambda *args: copy.deepcopy(self.result))
        self.evaluate = patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, endpoint, *, payload=None):
        if payload is None:
            if endpoint == 'user':
                return {'login': self.login}
            self.assertEqual(endpoint, 'repos/owner/repo/pulls/1')
            return copy.deepcopy(self.pr)
        self.posts.append((endpoint, payload))
        if endpoint.endswith('/reviews'):
            review = {'body': payload['body'], 'commit_id': payload['commit_id'],
                      'state': 'APPROVED', 'user': {'login': self.login},
                      'html_url': 'https://github.com/owner/repo/pull/1#pullrequestreview-1'}
            self.reviews.append(review)
            return copy.deepcopy(review)
        self.assertEqual(endpoint, 'repos/owner/repo/issues/1/comments')
        if self.fail_before_post:
            self.fail_before_post = False
            raise RuntimeError('connection failed before POST')
        comment = {'id': len(self.comments) + 1, 'body': payload['body'],
                   'user': {'login': self.login}, 'updated_at': '2026-10-30T18:00:00Z'}
        self.comments.append(comment)
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError('lost comment response')
        return copy.deepcopy(comment)

    def paginated(self, endpoint):
        if endpoint.endswith('/reviews?per_page=100'):
            return copy.deepcopy(self.reviews)
        if endpoint == 'repos/owner/repo/issues/1/comments?per_page=100':
            return copy.deepcopy(self.comments)
        self.assertEqual(endpoint, 'repos/owner/repo/pulls/1/comments?per_page=100')
        return []

    def run_review(self, **kwargs):
        r.review_one('owner/repo', 1, self.state, **kwargs)

    def restart(self):
        self.state = json.loads((self.root / 'state.json').read_text())

    def test_failed_overall_validation_still_publishes_verified_resolution(self):
        self.result['complete'] = True
        notifier = Mock()
        self.run_review(notifier=notifier)
        self.assertEqual(len(self.comments), 1)
        body = self.comments[0]['body']
        for text in ('**Resolved**', 'previous-date-finding', 'HTTP regression passes',
                     PR['head']['sha'], '**Review deferred:**', 'Approval is withheld.',
                     'Build/test validation: failed', 'PostgreSQL checks were skipped'):
            self.assertIn(text, body)
        self.assertNotIn('LGTM', body)
        self.assertNotIn(r.MARKER, body)
        self.assertFalse(self.reviews)
        self.assertIn('deferred_revision', self.state['owner/repo#1'])
        notifier.observe.assert_any_call('owner/repo', 1, deferred=True)
        self.assertTrue(all('approval_sha' not in call.kwargs for call in notifier.observe.call_args_list))

    def test_own_comment_does_not_wake_a_second_model_run_after_restart(self):
        self.run_review()
        self.restart()
        self.run_review()
        self.evaluate.assert_called_once()
        self.assertEqual(len(self.comments), 1)
        expected = {**self.pr, 'conversation_comments': self.comments, 'review_comments': []}
        self.assertEqual(self.state['owner/repo#1']['deferred_revision'], r.deferred_revision(expected))

    def test_new_context_can_complete_review_for_the_same_head(self):
        self.run_review()
        self.restart()
        self.pr['body'] = 'New independent validation evidence.'
        self.result.update(complete=True, _validation={'enabled': True, 'status': 'passed', 'jobs': []})
        self.run_review()
        self.run_review()
        self.assertEqual(self.evaluate.call_count, 2)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(self.reviews[0]['state'], 'APPROVED')

    def test_unverified_findings_are_reported_without_claiming_resolution(self):
        self.result['prior_finding_statuses'].append({
            'id': 'other-finding', 'status': 'unverified', 'evidence': 'Provider evidence is unavailable.',
        })
        self.run_review()
        self.assertIn('**Unverified** — `other-finding`', self.comments[0]['body'])
        self.assertIn('Provider evidence is unavailable.', self.comments[0]['body'])
        self.assertFalse(self.reviews)

    def test_no_verified_resolution_keeps_existing_silent_deferral(self):
        self.result['prior_finding_statuses'][0].update(status='unverified', evidence='Evidence unavailable.')
        self.run_review()
        self.assertFalse(self.posts)
        self.assertIn('deferred_revision', self.state['owner/repo#1'])

    def test_lost_post_response_is_deduplicated_and_remains_retryable(self):
        self.lose_response = True
        with self.assertRaisesRegex(RuntimeError, 'lost comment response'):
            self.run_review()
        self.restart()
        self.assertNotIn('deferred_revision', self.state['owner/repo#1'])
        self.run_review()
        self.restart()
        self.run_review()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.evaluate.call_count, 2)
        self.assertIn('deferred_revision', self.state['owner/repo#1'])

    def test_failed_post_is_retried_without_caching_away_the_update(self):
        self.fail_before_post = True
        with self.assertRaisesRegex(RuntimeError, 'connection failed before POST'):
            self.run_review()
        self.restart()
        self.assertNotIn('deferred_revision', self.state['owner/repo#1'])
        self.run_review()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.posts), 2)

    def test_reworded_evidence_is_deduplicated_from_remote_comment_after_state_loss(self):
        self.run_review()
        self.state = {}
        self.result['prior_finding_statuses'][0]['evidence'] = 'The same verified fix, phrased differently.'
        self.run_review()
        self.assertEqual(len(self.comments), 1)

    def test_changed_resolution_statuses_publish_an_updated_followup(self):
        self.result['prior_finding_statuses'].append({
            'id': 'second-finding', 'status': 'unverified', 'evidence': 'Validation evidence missing.',
        })
        self.run_review()
        self.pr['body'] = 'New evidence for the second finding.'
        self.result['prior_finding_statuses'][1].update(status='resolved', evidence='Second fix verified.')
        self.run_review()
        self.assertEqual(len(self.comments), 2)
        self.assertIn('**Resolved** — `second-finding`', self.comments[1]['body'])

    def test_other_users_marker_cannot_suppress_the_resolution_comment(self):
        update = r.resolution_followup(self.result, PR['head']['sha'])
        self.comments.append({'id': 10, 'body': update['body'], 'user': {'login': 'someone-else'}})
        self.run_review()
        self.assertEqual(len(self.comments), 2)
        self.assertEqual(self.comments[-1]['user']['login'], 'reviewer')

    def test_stale_closed_draft_or_unassigned_pr_cannot_receive_followup(self):
        changes = ({'head': {'sha': 'c' * 40}}, {'base': {'sha': 'c' * 40}},
                   {'state': 'closed'}, {'draft': True}, {'requested_reviewers': []})
        for change in changes:
            with self.subTest(change=change):
                self.pr, self.state = copy.deepcopy(PR), {}
                def assess(*args):
                    self.pr.update(change)
                    return copy.deepcopy(self.result)
                self.evaluate.side_effect = assess
                self.run_review()
                self.assertFalse(self.posts)
                self.assertNotIn('deferred_revision', self.state['owner/repo#1'])

    def test_changed_companion_cannot_receive_a_resolution_followup(self):
        companion = {'repository': 'owner/shared', 'number': 2, 'sha': 'c' * 40}
        self.result['_companion_prs'] = [companion]
        with patch.object(r, 'resolve_companion_pull', return_value={**companion, 'sha': 'd' * 40}):
            self.run_review()
        self.assertFalse(self.posts)
        self.assertNotIn('deferred_revision', self.state['owner/repo#1'])

    def test_account_or_scope_change_blocks_the_comment(self):
        for change in ('account', 'scope'):
            with self.subTest(change=change):
                self.state, self.login = {}, 'reviewer'
                r.CONFIG['repositories'] = ['owner/repo']
                def assess(*args):
                    if change == 'account':
                        self.login = 'other-account'
                    else:
                        r.CONFIG['repositories'] = []
                    return copy.deepcopy(self.result)
                self.evaluate.side_effect = assess
                with self.assertRaisesRegex(RuntimeError, 'scope or GitHub account changed'):
                    self.run_review()
                self.assertFalse(self.posts)
                self.assertNotIn('deferred_revision', self.state['owner/repo#1'])

    def test_dry_run_previews_without_posting_state_or_notifications(self):
        notifier = Mock()
        with self.assertLogs(r.LOG, level='INFO') as logs:
            self.run_review(dry_run=True, notifier=notifier)
        self.assertTrue(any('DRY RUN resolution follow-up' in line for line in logs.output))
        self.assertFalse(self.posts)
        self.assertEqual(self.state, {})
        self.assertFalse((self.root / 'state.json').exists())
        notifier.observe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
