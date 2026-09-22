import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from jc.cache import Cache
from jc.config import Config, load_config
from jc.core import Chunk, ask, request_body
from jc.commands import init, main
from jc.index import retrieve, inspect_symbols, source
from jc.mcp import Server, serve
from jc.runner import baseline, build_command, execute
from jc.service import Ranker, choose_model, prepare, read_json, route_rules, route_tools, write_json
from jc.telemetry import Observer, compare, evaluate, summarize_logs


class RepoTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'auth.py').write_text('import time\n\ndef refresh_token(token):\n    return token\n\nclass User:\n    pass\n')
        (self.repo / 'AGENTS.md').write_text('Always follow permission controls.\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'Initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE)

    def fake(self, body):
        file = self.home / ('fake-' + str(time.time_ns()))
        file.write_text('#!' + sys.executable + '\nimport json, sys, time, os\n' + body)
        file.chmod(0o700)
        return str(file)

    def run_baseline(self, body, **kwargs):
        out = baseline(self.repo, 'Fix refresh token')
        result = execute(self.repo, 'Fix refresh token', out, executable=self.fake(body), **kwargs)
        return out, result


class ConfigurationTests(RepoTest):
    def test_config_validation(self):
        for kwargs in ({'budget': True}, {'timeout': float('nan')}, {'candidates': 1.5}, {'excerpt_threshold': .9, 'full_threshold': .2}):
            with self.assertRaises(ValueError):
                Config(**kwargs).validate()
        cfg = self.home / 'config.toml'
        cfg.write_text('budget = 1234\ncandidates = 12\n')
        self.assertEqual(load_config(self.repo, str(cfg), budget=1400).budget, 1400)
        cfg.write_text('command = "untrusted-command"')
        with self.assertRaises(ValueError):
            load_config(self.repo, str(cfg))

    def test_init_preserves_agents(self):
        before = (self.repo / 'AGENTS.md').read_text()
        init(self.repo)
        self.assertEqual((self.repo / 'AGENTS.md').read_text(), before)
        self.assertTrue(load_config(self.repo).rules_manifest)
        with self.assertRaises(ValueError):
            init(self.repo)

    def test_ast_and_japanese_aliases(self):
        chunks, meta = retrieve(self.repo, '認証の期限', 10)
        self.assertIn('auth.py', {c.path for c in chunks})
        symbols = inspect_symbols(self.repo, 'refresh')
        self.assertEqual(symbols[0]['start'], 3)
        self.assertEqual(symbols[0]['end'], 4)

    def test_instruction_oversize_fails_closed(self):
        (self.repo / 'AGENTS.md').write_text('a' * 12001)
        with self.assertRaises(ValueError):
            retrieve(self.repo, 'auth')

    def test_path_escape(self):
        with self.assertRaises(ValueError):
            source(self.repo, '../outside')
        outside = self.home / 'outside'
        outside.write_text('secret')
        (self.repo / 'link').symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            source(self.repo, 'link/outside')


class RankingTests(RepoTest):
    def test_cache_exact_request_and_expiry(self):
        cache = Cache(self.home / 'cache.sqlite3', 60)
        self.addCleanup(cache.close)
        c = Chunk('c', 'a.py', 1, 1, 'value = 1')
        body = request_body('task', [c])
        cache.put(body, {'c': .8})
        self.assertEqual(cache.get(body), {'c': .8})
        self.assertIsNone(cache.get(request_body('other task', [c])))
        self.assertIsNone(cache.get(request_body('task', [c], 'different-model')))
        self.assertIsNone(cache.get(request_body('task', [replace(c, content='value = 2')])))
        self.assertIsNone(cache.get(request_body('task', [c], criterion='different criterion')))
        cache.ttl = 0
        self.assertIsNone(cache.get(body))

    def test_rank_cache_and_retry(self):
        cache = Cache(self.home / 'cache.sqlite3', 60)
        self.addCleanup(cache.close)
        chunk = Chunk('c', 'a.py', 1, 1, 'token')
        ranker = Ranker(Config(retries=1), cache)
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'unit-test-key'}), patch('jc.service.ask', side_effect=[TimeoutError(), ({'c': .8}, {'input_tokens': 10})]) as api:
            scores, failed = ranker.rank('task', [chunk])
            self.assertEqual(scores, {'c': .8})
            ranker.rank('task', [chunk])
            self.assertEqual(api.call_count, 2)
        self.assertEqual(ranker.stats['cache_hits'], 1)
        self.assertEqual(ranker.stats['api_failures'], 1)

    def test_real_http_adapter_with_mock_transport(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"answers":{"x":{"noul":0.75}},"usage":{"input_tokens":10}}'
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(timeout, 2)
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
                body = json.loads(request.data)
                self.assertIn('specific criterion', body['questions']['x']['instructions'])
                return Response()
        c = Chunk('x', 'source.py', 1, 1, 'token')
        with patch('urllib.request.build_opener', return_value=Opener()):
            scores, usage = ask('task', [c], 'test-key', timeout=2, criterion='specific criterion')
        self.assertEqual(scores['x'], .75)
        self.assertEqual(usage['input_tokens'], 10)

    def test_required_rules_and_optional_routing(self):
        init(self.repo)
        cfg = load_config(self.repo)
        ranker = Ranker(cfg)
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test'}), patch('jc.service.ask', side_effect=lambda task, chunks, *args: ({c.id: .01 for c in chunks}, {})):
            text, rows = route_rules(self.repo, cfg.rules_manifest, 'task', ranker)
        self.assertIn('permission', text)
        self.assertEqual(sum(r['selected'] for r in rows), 1)
        (self.repo / '.jev/rules.toml').write_text('[[rules]]\npath="AGENTS.md"\noptional=true\n')
        with self.assertRaises(ValueError):
            route_rules(self.repo, cfg.rules_manifest, 'task', ranker)

    def test_catalog_discovery_and_expand(self):
        catalog = self.repo / 'tools.json'
        catalog.write_text(json.dumps([{'name': f'tool{i}', 'description': f'Description {i}', 'inputSchema': {'type': 'object'}} for i in range(3)]))
        ranker = Ranker(Config(tool_limit=1), offline=True)
        self.assertEqual(len(route_tools(self.repo, 'tools.json', 'task', ranker)['tools']), 1)
        self.assertEqual(len(route_tools(self.repo, 'tools.json', 'task', ranker, True)['tools']), 3)

    def test_conservative_model_route(self):
        cfg = Config(small_model='small', large_model='large')
        ranker = Ranker(cfg, offline=True)
        self.assertEqual(choose_model('task', cfg, ranker)['model'], 'large')
        with patch.object(ranker, 'rank', return_value=({'simple-task': .99}, set())):
            self.assertEqual(choose_model('task', cfg, ranker)['model'], 'small')

    def test_prepare_budget_and_immutable_runs(self):
        init(self.repo)
        out, metrics, rows = prepare(self.repo, 'refresh token', load_config(self.repo), offline=True)
        self.assertLessEqual(metrics['pack_tokens_est'], 8000)
        self.assertEqual(metrics['api_calls'], 0)
        self.assertIn('permission', (out / 'current-rules.md').read_text())
        self.assertIn('current-rules.md', {p.name for p in out.iterdir()})
        with self.assertRaises(ValueError):
            prepare(self.repo, 'task', Config(), out, offline=True)


GOOD = '''prompt = sys.stdin.read()
assert prompt.startswith('Task:')
assert sys.argv[1:3] == ['exec', '--json']
print(json.dumps({'type':'item.completed','item':{'id':'a','type':'agent_message','text':'Completed fixture'}}), flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':123,'cached_input_tokens':23,'output_tokens':17}}), flush=True)
'''


class RunnerTests(RepoTest):
    def test_runner_real_subprocess_success(self):
        out, (code, metrics) = self.run_baseline(GOOD)
        self.assertEqual(code, 0)
        self.assertEqual(metrics['codex_input_tokens'], 123)
        self.assertEqual(metrics['codex_output_tokens'], 17)
        self.assertEqual(metrics['codex_status'], 'completed')
        self.assertIsNone(metrics['task_success'])
        self.assertEqual((out / 'final-message.md').read_text(), 'Completed fixture')

    def test_zero_exit_without_completion_is_failure(self):
        out, (code, metrics) = self.run_baseline("print('{}')\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(metrics['codex_status'], 'failed')
        self.assertIsNone(metrics['codex_input_tokens'])

    def test_turn_failure_not_success(self):
        out, (code, metrics) = self.run_baseline("print(json.dumps({'type':'turn.failed'}))\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(metrics['codex_status'], 'failed')

    def test_timeout_and_redaction(self):
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'fake-sensitive-key'}):
            out, (code, metrics) = self.run_baseline("print(json.dumps({'type':'item.completed','item':{'id':'x','type':'agent_message','text':os.environ['TYPESAFE_API_KEY']}}), flush=True)\ntime.sleep(10)\n", timeout=.3)
        self.assertEqual(code, 124)
        self.assertEqual(metrics['codex_status'], 'timeout')
        self.assertNotIn('fake-sensitive-key', (out / 'codex-events.jsonl').read_text())

    def test_loop_stop(self):
        body = "for i in range(6):\n    print(json.dumps({'type':'item.completed','item':{'id':str(i),'type':'command_execution','command':'rg auth','aggregated_output':'same'}}), flush=True)\ntime.sleep(10)\n"
        out, (code, metrics) = self.run_baseline(body, max_repeat=3)
        self.assertNotEqual(code, 0)
        self.assertEqual(metrics['codex_status'], 'loop_stopped')
        self.assertTrue(metrics['loop_warnings'])

    def test_dry_run_and_no_bypass(self):
        out = baseline(self.repo, 'task')
        code, metrics = execute(self.repo, 'task', out, executable='does-not-exist', dry_run=True)
        self.assertEqual(metrics['codex_status'], 'prepared')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', build_command())
        self.assertEqual(build_command()[-1], '-')
        with self.assertRaises(ValueError):
            build_command(sandbox='danger-full-access')

    def test_child_search_usage_link(self):
        body = "import subprocess\nsubprocess.run([sys.executable, '-m', 'jc', 'search', 'token', '--repo', os.getcwd(), '--offline', '--plain'], check=True, capture_output=True)\n" + GOOD
        out, (code, metrics) = self.run_baseline(body)
        self.assertEqual(code, 0)
        self.assertEqual(len(metrics['child_searches']), 1)
        self.assertTrue((out / 'child-searches.jsonl').exists())
        self.assertEqual(metrics['api_calls'], 0)

    def test_wrapper_e2e_offline(self):
        out = self.home / 'cli-run'
        code = main(['exec', 'refresh token', '--repo', str(self.repo), '--offline', '--output', str(out), '--codex', self.fake(GOOD), '--plain'])
        self.assertEqual(code, 0)
        self.assertIn('SOURCE', (out / 'prompt.md').read_text())
        self.assertEqual(read_json(out / 'metrics.json')['codex_input_tokens'], 123)


class TelemetryTests(RepoTest):
    def test_multi_turn_usage_and_unknown(self):
        observer = Observer()
        observer.feed({'type':'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 20}})
        observer.feed({'type':'turn.completed', 'usage': {'input_tokens': 30, 'output_tokens': 4}})
        self.assertEqual(observer.metrics()['codex_input_tokens'], 130)
        observer.feed({'type':'turn.completed'})
        self.assertIsNone(observer.metrics()['codex_input_tokens'])

    def test_log_classification(self):
        report = summarize_logs('setup\nSQLSTATE: database timeout\nend')
        self.assertEqual(report['category'], 'database')
        self.assertEqual(report['lines'][1]['line'], 2)

    def test_paired_evaluation(self):
        a, _ = self.run_baseline(GOOD)
        b, _, _ = prepare(self.repo, 'Fix refresh token', Config(), offline=True)
        execute(self.repo, 'Fix refresh token', b, executable=self.fake(GOOD))
        evaluate(a, True, 'Reviewed fixture')
        evaluate(b, False, 'Rejected fixture')
        report = compare([a], [b])
        self.assertEqual(report['baseline']['input_tokens_per_success'], 123)
        self.assertIsNone(report['harness']['input_tokens_per_success'])
        self.assertEqual(report['harness']['success_rate'], 0)
        m = read_json(b / 'metrics.json'); m['commit'] = 'other'; write_json(b / 'metrics.json', m)
        with self.assertRaises(ValueError):
            compare([a], [b])

    def test_cost_unknown_and_explicit_rates(self):
        from jc.telemetry import total_cost
        rates = {'codex_input': 2, 'codex_cached_input': .2, 'codex_output': 8, 'jev_input': .04, 'jev_output': .1}
        m = {'codex_input_tokens': 1000, 'codex_cached_input_tokens': 500, 'codex_output_tokens': 100, 'api_calls': 0}
        self.assertAlmostEqual(total_cost([m], rates), .0019)
        m['codex_cached_input_tokens'] = None
        self.assertIsNone(total_cost([m], rates))
        with self.assertRaises(ValueError):
            total_cost([m], {'codex_input': -1})
        o = Observer()
        o.feed({'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 10}})
        self.assertIsNone(o.metrics()['codex_cached_input_tokens'])

    def test_unfinished_cannot_be_accepted(self):
        out = baseline(self.repo, 'task')
        with self.assertRaises(ValueError):
            evaluate(out, True, 'unchecked')


class McpTests(RepoTest):
    def test_protocol_and_session_bound_read(self):
        server = Server(self.repo, Config(), offline=True)
        init_response = server.dispatch({'jsonrpc':'2.0', 'id':1, 'method':'initialize', 'params':{'protocolVersion':'2025-06-18'}})
        self.assertEqual(init_response['result']['protocolVersion'], '2025-06-18')
        self.assertIsNone(server.dispatch({'jsonrpc':'2.0','method':'notifications/initialized'}))
        result = server.call('jc_search', {'query':'token'})
        id = result['candidates'][0]['id']
        self.assertIn('SOURCE', server.call('jc_read', {'run_id':result['run_id'], 'chunk_id':id})['source'])
        with self.assertRaises(ValueError):
            server.call('jc_read', {'run_id':'../../etc', 'chunk_id':id})
        with self.assertRaises(ValueError):
            server.call('jc_search', {'query':'token', 'repo':'/etc'})

    def test_stdout_only_jsonrpc(self):
        source = io.StringIO('{bad json}\n' + json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'unknown'}}) + '\n' + json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list'}) + '\n')
        output = io.StringIO()
        serve(self.repo, Config(), True, source, output)
        rows = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(rows[0]['error']['code'], -32700)
        self.assertEqual(len(rows[2]['result']['tools']), 4)

    def test_stdio_subprocess(self):
        requests = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion':'2025-06-18'}),
                    dict(jsonrpc='2.0', id=2, method='tools/call', params={'name':'jc_symbol','arguments':{'query':'refresh'}})]
        result = subprocess.run([sys.executable,'-m','jc','serve','--repo',str(self.repo),'--offline'],
            input=''.join(json.dumps(x)+'\n' for x in requests), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertFalse(rows[1]['result']['isError'])
        self.assertIn('refresh_token', rows[1]['result']['content'][0]['text'])

    def test_benchmark_independent_worktrees(self):
        from jc.benchmark import benchmark
        manifest = self.home / 'tasks.json'
        manifest.write_text('["Fix refresh token"]')
        results = benchmark(self.repo, manifest, self.home / 'bench', Config(), executable=self.fake(GOOD), offline=True)
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0]['worktree'], results[1]['worktree'])
        report = compare([results[0]['run_dir']], [results[1]['run_dir']])
        self.assertEqual(report['input_reduction'], 0)
        self.assertFalse(report['baseline']['reviewed'])


if __name__ == '__main__':
    unittest.main()
