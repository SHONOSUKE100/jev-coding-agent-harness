from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4
from .core import Chunk, ask, estimate, retrieve, safe_display, save_run, select

COLORS = {'FULL': 121, 'PIN': 117, 'EXCERPT': 221, 'DROP': 203}


def show(rows, metrics, plain=False, delay=.15):
    animated = sys.stderr.isatty() and not plain and 'NO_COLOR' not in os.environ
    total = sum(r['before_est'] for r in rows)
    removed = 0
    for r in rows:
        score = ' —  ' if r['score'] is None else f'{r["score"]:.2f}'
        label = safe_display(f'{r["path"]}:{r["start"]}-{r["end"]}')[-43:]
        line = f'  {r["action"]:7} {score} {label:43} {r["before_est"]:5} → {r["after_est"]:5}'
        print(f'\033[38;5;{COLORS[r["action"]]}m{line}\033[0m' if animated else line, file=sys.stderr, flush=True)
        removed += r['before_est'] - r['after_est']
        if animated:
            fill = round(24 * (total - removed) / max(total, 1))
            print(f'  Context  {"█" * fill}{"░" * (24 - fill)}  ~{total - removed:,} source tokens', end='\r', file=sys.stderr, flush=True)
            time.sleep(delay)
            print('\033[2K', end='\r', file=sys.stderr)
    print(f'\n  SOURCES ~{metrics["source_before_est"]:,} → ~{metrics["source_after_est"]:,} tokens / {metrics["source_reduction_pct"]:.1f}% less\n'
          f'  PACK ~{metrics["pack_tokens_est"]:,} tokens including headers\n'
          f'  JEV {metrics["api_calls"]} requests / {metrics["api_failures"]} failures\n'
          '  ~ means byte-based estimate, NOT Codex billed usage.\n', file=sys.stderr)


def fixtures():
    specs = [('AGENTS.md', '# Rules\nPreserve security checks. Run tests.\n', None, True),
             ('src/auth/refresh.py', 'def refresh(token):\n    return decode(token)\n', .98, False),
             ('tests/test_refresh.py', 'def test_expired_refresh():\n    assert refresh(expired_token).status == 401\n', .96, False),
             ('src/db/users.py', 'def get_user(user_id):\n    return database.get(user_id)\n', .62, False),
             ('src/web/theme.ts', 'export const background = "lavender";\n', .08, False),
             ('docs/releases.md', 'Release notes: appearance and onboarding.\n', .17, False)]
    chunks, scores = [], {}
    for i, (path, body, score, pinned) in enumerate(specs):
        content = body if pinned else body * 15
        chunks.append(Chunk(f'demo-{i}', path, 1, len(content.splitlines()), content, pinned=pinned))
        if score is not None:
            scores[f'demo-{i}'] = score
    return chunks, scores


def run(args):
    started = time.monotonic()
    task = 'Fix expired refresh token returning HTTP 500' if args.command == 'demo' else args.task
    print('\n  JEV / WORK   Context observatory\n  ' + ('DEMO · scripted scores · no API' if args.command == 'demo' else 'LIVE · source excerpts sent to TypeSafe') + '\n  ' + safe_display(task), file=sys.stderr)
    usage, fallback, calls, failures = [], set(), 0, 0
    if args.command == 'demo':
        chunks, scores = fixtures()
        retrieval = {'demo': True}
    else:
        key = os.environ.get('TYPESAFE_API_KEY', '')
        if not key:
            raise ValueError('Set TYPESAFE_API_KEY, or use demo for offline playback')
        if len(task.encode()) > 8000:
            raise ValueError('Task exceeds 8 KB limit')
        chunks, retrieval = retrieve(Path(args.repo), task, args.candidates)
        if not chunks:
            raise ValueError('No eligible tracked source files found')
        scores = {}
        pending = [c for c in chunks if not c.pinned]
        print(f'  {len(chunks)} candidates. Instructions pinned.', file=sys.stderr)
        for offset in range(0, len(pending), 3):
            batch = pending[offset:offset + 3]
            calls += 1
            print(f'  Jev batch {calls}: {len(batch)} chunks …', file=sys.stderr, flush=True)
            try:
                judged, used = ask(task, batch, key)
                scores.update(judged)
                usage.append(used)
            except Exception as exc:
                failures += 1
                fallback.update(c.id for c in batch)
                print(f'  API fallback ({type(exc).__name__}); retain within budget.', file=sys.stderr)
    pack, rows = select(chunks, scores, args.budget, task, fallback)
    before, after = (sum(r[k] for r in rows) for k in ('before_est', 'after_est'))
    metrics = {'mode': args.command, 'source_before_est': before, 'source_after_est': after,
               'source_reduction_pct': (1 - after / before) * 100 if before else 0,
               'pack_tokens_est': estimate(pack), 'budget_tokens_est': args.budget,
               'estimator': 'ceil(UTF-8 bytes / 3); not a tokenizer', 'api_calls': calls,
               'api_failures': failures, 'jev_reported_usage_by_batch': usage,
               'elapsed_seconds': round(time.monotonic() - started, 4),
               'codex_input_tokens': None, 'task_success': None, 'retrieval': retrieval}
    show(rows, metrics, args.plain, args.delay)
    out = Path(args.output) if args.output else Path.cwd() / 'jev-runs' / uuid4().hex[:12]
    save_run(out, task, chunks, pack, rows, metrics)
    print(str((out / 'context-pack.md').resolve()))
    return 0


def main():
    parser = argparse.ArgumentParser(description='Visualize Jev repository context selection')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('demo', 'search'):
        p = sub.add_parser(name)
        if name == 'search':
            p.add_argument('task')
            p.add_argument('--repo', required=True)
            p.add_argument('--candidates', type=int, default=40)
        p.add_argument('--budget', type=int, default=8000)
        p.add_argument('--output', help='New output directory')
        p.add_argument('--plain', action='store_true')
        p.add_argument('--delay', type=float, default=.15)
    p = sub.add_parser('replay')
    p.add_argument('run_dir')
    p.add_argument('--plain', action='store_true')
    p = sub.add_parser('read')
    p.add_argument('run_dir')
    p.add_argument('chunk_id')
    args = parser.parse_args()
    try:
        if args.command == 'replay':
            root = Path(args.run_dir)
            print('REPLAY · recorded decisions · no API calls', file=sys.stderr)
            show(json.loads((root / 'decisions.json').read_text()), json.loads((root / 'metrics.json').read_text()), args.plain)
            return 0
        if args.command == 'read':
            chunks = json.loads((Path(args.run_dir) / 'chunks.json').read_text())
            c = next((c for c in chunks if c['id'] == args.chunk_id), None)
            if c is None:
                raise ValueError('Unknown chunk id')
            content = Chunk(**c).block()
            if sys.stdout.isatty():
                content = '\n'.join(safe_display(line) for line in content.splitlines())
            print(content)
            return 0
        if not 256 <= args.budget <= 200_000 or not 0 <= args.delay <= 1:
            raise ValueError('Require budget 256–200000 and delay 0–1')
        if args.command == 'search' and not 1 <= args.candidates <= 200:
            raise ValueError('Require candidates 1–200')
        if args.output and Path(args.output).exists():
            raise ValueError('Output already exists; choose a new directory')
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print('Error: ' + safe_display(str(exc)), file=sys.stderr)
        return 1
