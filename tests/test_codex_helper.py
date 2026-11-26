import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = str(Path(__file__).resolve().parents[1] / 'helpers' / 'codex-account')
loader = importlib.machinery.SourceFileLoader('codex_account_helper', HELPER)
m = importlib.util.module_from_spec(importlib.util.spec_from_loader('codex_account_helper', loader))
loader.exec_module(m)


class ResumeStartupTests(unittest.TestCase):
    def test_resumed_cli_never_stops_on_the_update_screen(self):
        cmd = m.resume_command({'cwd': '/work/proj'}, 'tid', 'continue')
        self.assertIn('check_for_update_on_startup=false', cmd)
        self.assertEqual(cmd[-1], 'continue')

    def test_project_is_trusted_once_so_the_trust_question_never_takes_the_prompt(self):
        # Codex CLI 0.167 asks "Trust this folder?" in a project not yet trusted; the prompt was lost behind it
        home = tempfile.mkdtemp()
        with open(os.path.join(home, 'config.toml'), 'w') as f:
            f.write('model = "m"\n\n[projects."/work/other"]\ntrust_level = "trusted"\n')
        self.assertTrue(m.ensure_trusted('/work/proj', home))
        self.assertFalse(m.ensure_trusted('/work/proj', home))
        self.assertFalse(m.ensure_trusted('/work/other', home))
        with open(os.path.join(home, 'config.toml')) as f:
            text = f.read()
        self.assertTrue(text.startswith('model = "m"'))
        self.assertEqual(text.count('[projects."/work/proj"]\ntrust_level = "trusted"'), 1)


if __name__ == '__main__':
    unittest.main()
