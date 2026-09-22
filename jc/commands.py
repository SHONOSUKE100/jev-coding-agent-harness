"""Command routing; the reusable pipeline never prints to stdout."""
import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from .cache import Cache
from .config import load_config
from .core import safe_display
from .index import inspect_symbols, root_path
from .service import Ranker, atomic_write, choose_model, prepare, read_chunk, read_json, route_rules, route_tools, state_dir, write_json


def progress(message):
    print('  ' + safe_display(message), file=sys.stderr, flush=True)


def display(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if sys.stdout.isatty():
        text = '\n'.join(safe_display(line) for line in text.splitlines())
    print(text)


def parser():
    p = argparse.ArgumentParser(description='Jev coding agent harness: selection, Codex execution, MCP and evaluation')
    p.add_argument('--version', action='version', version='jc 0.2.0')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('search', 'exec', 'rules', 'tools', 'route-model'):
        s = sub.add_parser(name)
        s.add_argument('task')
        common(s)
        s.add_argument('--budget', type=int)
        s.add_argument('--candidates', type=int)
        s.add_argument('--expand', action='store_true')
        s.add_argument('--no-cache', action='store_true')
        s.add_argument('--output')
        s.add_argument('--plain', action='store_true')
        s.add_argument('--delay', type=float, default=.08)
        if name == 'exec':
            execution(s)
            s.add_argument('--baseline', action='store_true')
            s.add_argument('--auto-model', action='store_true')
            s.add_argument('--max-repeat', type=int, default=0, help='Stop after N identical command/output pairs; 0 only warns')
    s = sub.add_parser('demo')
    s.add_argument('--budget', type=int, default=8000)
    s.add_argument('--output')
    s.add_argument('--plain', action='store_true')
    s.add_argument('--delay', type=float, default=.15)
    s = sub.add_parser('replay')
    s.add_argument('run_dir')
    s.add_argument('--plain', action='store_true')
    s = sub.add_parser('read')
    s.add_argument('run_dir')
    s.add_argument('chunk_id')
    s = sub.add_parser('symbol')
    s.add_argument('query')
    s.add_argument('--repo', default='.')
    s = sub.add_parser('serve')
    common(s)
    s = sub.add_parser('doctor')
    common(s)
    s.add_argument('--codex', default='codex')
    s = sub.add_parser('init')
    s.add_argument('--repo', default='.')
    s = sub.add_parser('log')
    s.add_argument('file')
    s = sub.add_parser('evaluate')
    s.add_argument('run_dir')
    s.add_argument('--success', choices=('true', 'false'), required=True)
    s.add_argument('--note', required=True)
    s = sub.add_parser('compare')
    s.add_argument('--baseline', nargs='+', required=True)
    s.add_argument('--harness', nargs='+', required=True)
    s.add_argument('--prices', help='JSON with explicit USD-per-million token rates')
    s.add_argument('--output')
    s = sub.add_parser('bench')
    s.add_argument('manifest')
    s.add_argument('--output', required=True)
    common(s)
    execution(s)
    return p


def common(p):
    p.add_argument('--repo', default='.')
    p.add_argument('--config', help='Explicit TOML config (otherwise .jev/config.toml)')
    p.add_argument('--offline', action='store_true', help='No Jev calls; retain candidates within budget')


def execution(p):
    p.add_argument('--codex', default='codex', help='Executable path, not a shell command')
    p.add_argument('--model')
    p.add_argument('--sandbox', choices=('read-only', 'workspace-write'), default='read-only')
    p.add_argument('--timeout', type=float, default=900)
    p.add_argument('--dry-run', action='store_true', help='Prepare prompt and command, do not launch Codex')


def init(repo):
    repo = root_path(repo)
    directory = repo / '.jev'
    if directory.exists():
        raise ValueError('.jev already exists; existing configuration is never overwritten')
    directory.mkdir(mode=0o700)
    (directory / 'rules').mkdir()
    atomic_write(directory / 'config.toml', '''# Data-only config; Codex executable and sandbox are explicit CLI options.
budget = 8000
candidates = 40
full_threshold = 0.8
excerpt_threshold = 0.4
model = "jev-latest"
timeout = 20
retries = 1
cache_ttl = 86400
rules_manifest = ".jev/rules.toml"
tools_catalog = ".jev/tools.json"
tool_limit = 5
# Set actual available model names to opt into --auto-model:
# small_model = "..."
# large_model = "..."
routing_threshold = 0.95
''')
    atomic_write(directory / 'rules.toml', '''# Required by default; only explicitly optional guidance may be omitted.
[[rules]]
path = ".jev/rules/base.md"
[[rules]]
path = ".jev/rules/backend.md"
optional = true
[[rules]]
path = ".jev/rules/frontend.md"
optional = true
''')
    atomic_write(directory / 'rules/base.md', 'Preserve applicable repository instructions and permission controls. Verify changes with relevant tests.\n')
    atomic_write(directory / 'rules/backend.md', 'For backend tasks, review input validation, error behavior and database transaction boundaries.\n')
    atomic_write(directory / 'rules/frontend.md', 'For frontend tasks, review keyboard interaction, small screens and loading/error states.\n')
    atomic_write(directory / 'tools.json', '[]\n')
    return {'config': str(directory / 'config.toml'), 'note': 'AGENTS.md and Codex settings were not modified'}


def selected_route(repo, config, args):
    cache = None if args.no_cache or args.offline else Cache(state_dir(repo) / 'cache.sqlite3', config.cache_ttl)
    ranker = Ranker(config, cache, args.offline, progress)
    try:
        if args.command == 'rules':
            text, decisions = route_rules(repo, config.rules_manifest, args.task, ranker)
            result = {'text': text, 'decisions': decisions}
        elif args.command == 'tools':
            result = route_tools(repo, config.tools_catalog, args.task, ranker, args.expand)
        else:
            result = choose_model(args.task, config, ranker)
        return {**result, 'metrics': ranker.stats}
    finally:
        if cache:
            cache.close()


def dispatch(args):
    from .cli import run, show
    from .runner import baseline, execute
    from .telemetry import compare, evaluate, summarize_logs
    command = args.command
    if hasattr(args, 'delay') and not 0 <= args.delay <= 1:
        raise ValueError('Delay must be 0–1')
    if command == 'demo':
        if not 256 <= args.budget <= 200000:
            raise ValueError('Budget must be 256–200000')
        if args.output and Path(args.output).exists():
            raise ValueError('Output already exists')
        return run(args)
    if command == 'read':
        display(read_chunk(args.run_dir, args.chunk_id))
        return 0
    if command == 'replay':
        directory = Path(args.run_dir)
        metrics = read_json(directory / 'metrics.json')
        if (directory / 'decisions.json').exists():
            show(read_json(directory / 'decisions.json'), metrics, args.plain)
        if 'codex_status' in metrics:
            display({k: metrics.get(k) for k in ('codex_status', 'codex_input_tokens', 'codex_output_tokens', 'tool_calls', 'loop_warnings')})
        return 0
    if command == 'evaluate':
        display(evaluate(args.run_dir, args.success == 'true', args.note))
        return 0
    if command == 'compare':
        report = compare(args.baseline, args.harness, read_json(Path(args.prices)) if args.prices else None)
        if args.output:
            path = Path(args.output)
            if path.exists():
                raise ValueError('Report already exists')
            write_json(path, report)
        display(report)
        return 0
    if command == 'log':
        path = Path(args.file)
        if path.stat().st_size > 10000000:
            raise ValueError('Log exceeds 10 MB')
        display(summarize_logs(path.read_text(errors='replace')))
        return 0
    repo = root_path(args.repo)
    if command == 'init':
        display(init(repo))
        return 0
    if command == 'symbol':
        display(inspect_symbols(repo, args.query))
        return 0
    config = load_config(repo, args.config, budget=getattr(args, 'budget', None), candidates=getattr(args, 'candidates', None))
    if command == 'doctor':
        display({'python': sys.version.split()[0], 'repository': str(repo), 'codex_executable': shutil.which(args.codex),
                 'jev_key_set': bool(os.environ.get('TYPESAFE_API_KEY')), 'config': asdict(config),
                 'authentication': 'not tested; run codex login status locally'})
        return 0
    if command == 'serve':
        from .mcp import serve
        return serve(repo, config, args.offline)
    if command in {'rules', 'tools', 'route-model'}:
        result = selected_route(repo, config, args)
        if args.output:
            path = Path(args.output)
            if path.exists():
                raise ValueError('Output already exists')
            write_json(path, result)
        display(result)
        return 0
    if command == 'bench':
        from .benchmark import benchmark
        display(benchmark(repo, args.manifest, args.output, config, args.codex, args.model, args.sandbox, args.offline, args.timeout, args.dry_run, progress))
        return 0
    if command == 'exec':
        if args.auto_model and (args.model or args.baseline):
            raise ValueError('--auto-model cannot be combined with --model or --baseline')
        if not 0 < args.timeout <= 86400 or args.max_repeat < 0:
            raise ValueError('Invalid timeout/repeat limits')
    if command == 'exec' and args.baseline:
        out = baseline(repo, args.task, args.output)
    else:
        out, metrics, rows = prepare(repo, args.task, config, args.output, args.offline, args.no_cache, args.expand, progress)
        show(rows, metrics, args.plain, args.delay)
    if command == 'search':
        display(str(out / 'context-pack.md'))
        return 0
    model = args.model
    if args.auto_model:
        original = args.command
        args.command = 'route-model'
        try:
            routing = selected_route(repo, config, args)
        finally:
            args.command = original
        model = routing['model']
        write_json(out / 'model-routing.json', routing)
        metrics = read_json(out / 'metrics.json')
        for key in ('api_calls', 'api_failures', 'cache_hits', 'cache_misses'):
            metrics[key] = metrics.get(key, 0) + routing['metrics'][key]
        metrics['jev_reported_usage_by_batch'] += routing['metrics']['jev_reported_usage_by_batch']
        write_json(out / 'metrics.json', metrics)
    code, metrics = execute(repo, args.task, out, args.codex, model, args.sandbox, args.timeout,
                            args.max_repeat, args.offline, args.dry_run, args.config, progress)
    if (out / 'final-message.md').exists():
        display((out / 'final-message.md').read_text())
    progress(f'Run: {out} · {metrics["codex_status"]} · input tokens: {metrics.get("codex_input_tokens")}')
    display(str(out))
    return code


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        progress('Error: ' + str(exc))
        return 1
