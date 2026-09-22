from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4
from .core import Chunk, estimate, safe_display, save_run, select

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
    task = 'Fix expired refresh token returning HTTP 500'
    print('\n  JEV / WORK   Context observatory\n  DEMO · scripted scores · no API', file=sys.stderr)
    chunks, scores = fixtures()
    pack, rows = select(chunks, scores, args.budget, task)
    before, after = (sum(r[k] for r in rows) for k in ('before_est', 'after_est'))
    metrics = {'mode': 'demo', 'source_before_est': before, 'source_after_est': after,
               'source_reduction_pct': (1 - after / before) * 100 if before else 0,
               'pack_tokens_est': estimate(pack), 'api_calls': 0, 'api_failures': 0,
               'codex_input_tokens': None, 'task_success': None}
    show(rows, metrics, args.plain, args.delay)
    out = Path(args.output) if args.output else Path.cwd() / 'jev-runs' / uuid4().hex[:12]
    save_run(out, task, chunks, pack, rows, metrics)
    print(str((out / 'context-pack.md').resolve()))
    return 0


def main():
    from .commands import main as command_main
    return command_main()


def search_main():
    sys.argv.insert(1, 'search')
    return main()


def read_main():
    sys.argv.insert(1, 'read')
    return main()


def symbol_main():
    sys.argv.insert(1, 'symbol')
    return main()
