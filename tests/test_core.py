import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from jc.core import Chunk, estimate, retrieve, select, validate_answers, request_body, ask, safe_display
from jc.cli import main


class CoreTests(unittest.TestCase):
    def chunk(self, id='x', pinned=False):
        return Chunk(id, 'src/auth.py', 1, 60, ''.join(f'line {i} token\n' for i in range(60)), pinned=pinned)

    def test_decisions_and_budget(self):
        chunks = [self.chunk(str(i)) for i in range(3)]
        pack, rows = select(chunks, {'0': .9, '1': .6, '2': .1}, 2000, 'token')
        self.assertEqual([r['action'] for r in rows], ['FULL', 'EXCERPT', 'DROP'])
        self.assertIn(chunks[0].content, pack)
        self.assertLessEqual(estimate(pack), 2000)

    def test_pin_not_scored_or_discarded(self):
        c = self.chunk(pinned=True)
        pack, rows = select([c], {c.id: 0}, 2000, 'task')
        self.assertEqual(rows[0]['action'], 'PIN')
        self.assertIn(c.content, pack)
        with self.assertRaises(ValueError):
            select([c], {}, 256, 'task')

    def test_malformed_and_missing_answers(self):
        c = self.chunk()
        for value in [None, True, -1, 1.1, float('nan'), '0.9']:
            with self.assertRaises(ValueError):
                validate_answers({'answers': {'x': {'noul': value}}}, [c])
        with self.assertRaises(ValueError):
            validate_answers({'answers': {}}, [c])
        self.assertEqual(validate_answers({'answers': {'x': {'noul': .7}}}, [c]), {'x': .7})

    def test_fallback(self):
        c = self.chunk()
        pack, rows = select([c], {}, 2000, 'task', {'x'})
        self.assertEqual(rows[0]['reason'], 'api-fallback')
        self.assertIn(c.content, pack)

    def test_hard_budget(self):
        pack, rows = select([self.chunk(str(i)) for i in range(10)], {str(i): 1 for i in range(10)}, 400, 'token')
        self.assertLessEqual(estimate(pack), 400)
        self.assertTrue(any(r['reason'] == 'budget' for r in rows))

    def test_tracked_sources_symlinks_and_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(['git', 'init', '-q', td], check=True)
            (root / 'auth.py').write_text('def refresh_token(): pass\n')
            (root / '.env').write_text('secret')
            (root / 'leak.py').write_text('api_key = "abcdefghijklmno"')
            (root / 'AGENTS.md').write_text('All instructions must remain.')
            (root / 'link.py').symlink_to('/etc/passwd')
            subprocess.run(['git', '-C', td, 'add', '.'], check=True)
            (root / 'untracked.py').write_text('token')
            chunks, stats = retrieve(root, 'refresh token')
            self.assertEqual({c.path for c in chunks}, {'auth.py', 'AGENTS.md'})

    def test_request_schema(self):
        self.assertEqual(request_body('task', [self.chunk()])['questions']['x']['type'], 'noul')

    def test_no_key(self):
        with self.assertRaises(ValueError):
            ask('task', [], '')

    def test_controls(self):
        self.assertNotIn('\x1b', safe_display('x\x1b[2J'))

    def test_live_success_and_api_failure(self):
        for fail in (False, True):
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / 'run'
                subprocess.run(['git', 'init', '-q', td], check=True)
                subprocess.run(['git', '-C', td, '-c', 'user.name=Test', '-c', 'user.email=test@local', 'commit', '--allow-empty', '-qm', 'Initial'], check=True)
                chunk = self.chunk()
                response = {'x': .93}, {'input_tokens': 123, 'output_tokens': 4}
                with patch('jc.service.retrieve', return_value=([chunk], {})), \
                     patch('jc.service.ask', side_effect=TimeoutError() if fail else None, return_value=response), \
                     patch.dict('os.environ', {'TYPESAFE_API_KEY': 'test-only'}), \
                     patch('sys.argv', ['jc', 'search', 'token', '--repo', td, '--plain', '--output', str(out)]):
                    self.assertEqual(main(), 0)
                metrics = json.loads((out / 'metrics.json').read_text())
                self.assertEqual(metrics['api_failures'], 2 if fail else 0)
                self.assertIn(chunk.content, (out / 'context-pack.md').read_text())
                self.assertNotIn('test-only', ''.join(p.read_text() for p in out.iterdir()))

    def test_demo_and_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / 'demo'
            with patch('sys.argv', ['jc', 'demo', '--plain', '--output', str(out)]):
                self.assertEqual(main(), 0)
            metrics = json.loads((out / 'metrics.json').read_text())
            self.assertEqual(metrics['api_calls'], 0)
            self.assertIsNone(metrics['codex_input_tokens'])
            with patch('sys.argv', ['jc', 'replay', str(out), '--plain']):
                self.assertEqual(main(), 0)
            with patch('sys.argv', ['jc', 'demo', '--output', str(out)]):
                self.assertEqual(main(), 1)


if __name__ == '__main__':
    unittest.main()
