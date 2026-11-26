import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import agy_model
from quota import normalize

ALL = ['Gemini 4 Neon', 'Gemini 4 Carbon', 'Gemini 4 Argon', 'Gemini 4 Hydrogen', 'Gemini 3.8 Flash']
FEW = ['Gemini 4 Hydrogen', 'Gemini 3.8 Flash']


class AgyModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, 'settings.json')
        self.state = os.path.join(self.tmp.name, 'agy_model.json')
        self.set_model('Gemini 4 Carbon (High)')

    def set_model(self, model):
        with open(self.settings, 'w') as f:
            json.dump({'colorScheme': 'dark', 'model': model, 'trustedWorkspaces': ['/w']}, f)

    def model(self):
        with open(self.settings) as f:
            return json.load(f)

    def fit(self, models):
        return agy_model.fit(models, self.state, self.settings)

    def test_quota_summary_lists_each_groups_models(self):
        groups = normalize({'groups': [
            {'displayName': 'Gemini Models', 'description': 'Models within this group: Gemini 4 Hydrogen, Gemini 3.8 Flash',
             'buckets': [{'window': '5h', 'remainingFraction': 1}]},
            {'displayName': 'Claude and GPT models', 'buckets': [{'window': '5h', 'remainingFraction': 1}]}]})
        self.assertEqual(groups[0]['models'], FEW)
        self.assertEqual(groups[1]['models'], [])

    def test_an_account_without_the_model_gets_hydrogen_at_the_same_effort(self):
        # 2026-11-26: some accounts offered only Gemini 4 Hydrogen / 3.8 Flash; agy refused Carbon with exit 1
        self.assertEqual(self.fit(FEW), ('Gemini 4 Carbon (High)', 'Gemini 4 Hydrogen (High)'))
        self.assertEqual(self.model()['model'], 'Gemini 4 Hydrogen (High)')
        self.assertEqual(self.model()['trustedWorkspaces'], ['/w'])  # the rest of agy's settings stay

    def test_the_chosen_model_comes_back_on_an_account_that_has_it(self):
        self.fit(FEW)
        self.fit(FEW)  # another account without it: stays on Hydrogen
        self.assertEqual(self.fit(ALL), ('Gemini 4 Hydrogen (High)', 'Gemini 4 Carbon (High)'))

    def test_a_model_picked_by_hand_meanwhile_becomes_the_chosen_one(self):
        self.fit(FEW)
        self.set_model('Gemini 3.8 Flash (Low)')
        self.assertIsNone(self.fit(ALL))
        self.assertEqual(self.model()['model'], 'Gemini 3.8 Flash (Low)')

    def test_nothing_changes_while_the_account_has_the_model_or_its_models_are_unknown(self):
        self.assertIsNone(self.fit(ALL))
        self.assertIsNone(self.fit([]))
        self.assertEqual(self.model()['model'], 'Gemini 4 Carbon (High)')

    def test_flash_never_gets_an_effort_it_does_not_have(self):
        self.set_model('Gemini 4 Neon (XHigh)')
        self.assertEqual(self.fit(['Gemini 3.8 Flash'])[1], 'Gemini 3.8 Flash (High)')
        self.assertEqual(self.fit(FEW)[1], 'Gemini 4 Hydrogen (XHigh)')


if __name__ == '__main__':
    unittest.main()
