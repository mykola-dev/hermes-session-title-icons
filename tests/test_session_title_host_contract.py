"""Pinned Hermes checks, on synthetic storage only (no production mounts)."""
import ast
import tempfile
import unittest
from pathlib import Path

class HostContractTests(unittest.TestCase):
    def setUp(self):
        from hermes_state import SessionDB
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = SessionDB(Path(tmp.name) / 'state.db')
        self.addCleanup(self.db.close)
        self.db.create_session('synthetic', 'cli')

    def test_direct_manual_title_change_does_not_advance_runtime_fences(self):
        self.db.set_session_title('synthetic', 'Before')
        before = self.db.get_session('synthetic')
        self.db.set_session_title('synthetic', 'Manual rename')
        after = self.db.get_session('synthetic')
        self.assertNotEqual(before['title'], after['title'])
        for field in ('runtime_revision', 'runtime_generation'):
            self.assertEqual(before[field], after[field])

    def test_auto_title_upgrade_does_not_advance_runtime_fences(self):
        self.db.set_auto_title('synthetic', 'Derived', source='derived')
        before = self.db.get_session('synthetic')
        self.db.set_auto_title('synthetic', 'Finished model title', source='llm')
        after = self.db.get_session('synthetic')
        self.assertNotEqual(before['title'], after['title'])
        for field in ('runtime_revision', 'runtime_generation'):
            self.assertEqual(before[field], after[field])

    def test_rest_rename_does_not_forward_expected_title(self):
        tree = ast.parse(Path('/opt/hermes/hermes_cli/web_routers/sessions.py').read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'rename_session_endpoint')
        call = next(n for n in ast.walk(node) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == '_mutate_session_request')
        self.assertEqual({kw.arg for kw in call.keywords}, {
            'request_id', 'expected_revision', 'expected_generation', 'operation', 'payload'})

    def test_host_does_not_preserve_all_emoji_sequences(self):
        self.assertNotEqual(self.db.sanitize_title('👨‍💻 Programming'), '👨‍💻 Programming')


if __name__ == '__main__':
    unittest.main()
