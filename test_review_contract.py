import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reviewer as r
from test_reviewer import CLEAN, PR


class ReviewContractTests(unittest.TestCase):
    def check_contract(self, prior_count):
        original_schema = (Path(__file__).parent / 'review-schema.json').read_text()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'work').mkdir()
            (root / 'review-schema.json').write_text(original_schema)
            (root / 'review-prompt.md').write_text('Review the current source.')
            history = {'round': prior_count + 1, 'prior_findings': [
                {'id': f'review-{n}-finding-1'} for n in range(1, prior_count + 1)]}
            context = {'review_history': history,
                       'companion_evidence': {'pull_requests': [], 'unavailable': []}}
            ids = [item['id'] for item in history['prior_findings']]

            def command(argv, *, input_text, **kwargs):
                self.assertEqual(argv[argv.index('--model') + 1], 'gpt-6-luna')
                self.assertIn('model_reasoning_effort="max"', argv)
                schema = json.loads(Path(argv[argv.index('--output-schema') + 1]).read_text())
                contract = schema['properties']['prior_finding_statuses']
                self.assertEqual((contract['minItems'], contract['maxItems']),
                                 (prior_count, prior_count))
                if ids:
                    self.assertEqual(contract['items']['properties']['id']['enum'], ids)
                    self.assertIn(json.dumps(ids), input_text)
                else:
                    self.assertNotIn('enum', contract['items']['properties']['id'])
                data = copy.deepcopy(CLEAN)
                data['prior_finding_statuses'] = [{'id': identifier, 'status': 'unverified',
                    'evidence': 'The required provider evidence is unavailable.'} for identifier in ids]
                Path(argv[argv.index('--output-last-message') + 1]).write_text(json.dumps(data))

            config = {'codex': 'codex', 'model': 'configured-model',
                      'reasoning': 'xhigh', 'skill_files': []}
            with patch.object(r, 'ROOT', root), patch.object(r, 'CONFIG', config), \
                    patch.object(r, 'prepare_context', return_value=context), \
                    patch.object(r, 'run_checks', return_value={'status': 'passed'}), \
                    patch.object(r, 'review_model', return_value=('gpt-6-luna', 'max')) as select_model, \
                    patch.object(r, 'command', side_effect=command):
                result = r.evaluate('owner/repo', 1, PR, [])
                payload = r.review_body(result, PR['head']['sha'])
                select_model.assert_called_once()
            if prior_count:
                self.assertFalse(result['complete'])
                self.assertIsNone(payload)
            else:
                self.assertEqual(payload['event'], 'APPROVE')
            self.assertEqual((root / 'review-schema.json').read_text(), original_schema)
            saved = json.loads((root / 'last-review.json').read_text())
            self.assertEqual((saved['model'], saved['reasoning']), ('gpt-6-luna', 'max'))

    def test_round_six_requires_all_prior_ids_and_cannot_approve_unverified_findings(self):
        self.check_contract(10)

    def test_first_review_has_empty_prior_statuses_without_invalid_empty_enum(self):
        self.check_contract(0)


if __name__ == '__main__':
    unittest.main()
