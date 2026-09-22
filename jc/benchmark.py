"""Sequential A/B trials in two distinct Git worktrees at the same commit."""
from pathlib import Path
from .index import git, root_path
from .runner import baseline, execute
from .service import prepare, read_json, write_json


def benchmark(repo, manifest, output, config, executable='codex', model=None, sandbox='read-only', offline=False, timeout=900, dry_run=False, progress=lambda msg: None):
    repo = root_path(repo)
    if git(repo, 'status', '--porcelain').strip():
        raise ValueError('Benchmark requires a clean worktree, including untracked files')
    tasks = read_json(Path(manifest))
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 50:
        raise ValueError('Benchmark manifest must contain 1–50 task strings')
    if any(not isinstance(t, str) or not t.strip() or len(t.encode()) > 8000 for t in tasks):
        raise ValueError('Invalid benchmark task')
    if len(set(tasks)) != len(tasks):
        raise ValueError('Benchmark tasks must be unique')
    output = Path(output).resolve()
    if output.is_relative_to(repo):
        raise ValueError('Benchmark output must be outside source repository')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
    results = []
    for i, task in enumerate(tasks):
        # Alternate arm order to reduce a systematic first-run effect.
        for arm in (('baseline', 'harness') if i % 2 == 0 else ('harness', 'baseline')):
            worktree = output / f'worktree-{i}-{arm}'
            run_dir = output / f'run-{i}-{arm}'
            git(repo, 'worktree', 'add', '--detach', str(worktree), commit)
            # Keep worktrees for diff review and independent acceptance evaluation.
            # No deletion, force reset, or implicit dependency installation.
            if arm == 'baseline':
                baseline(worktree, task, run_dir)
            else:
                prepare(worktree, task, config, run_dir, offline=offline, no_cache=True, progress=progress)
            code, metrics = execute(worktree, task, run_dir, executable=executable, model=model,
                                    sandbox=sandbox, timeout=timeout, offline=offline, dry_run=dry_run, progress=progress)
            results.append({'task': task, 'arm': arm, 'run_dir': str(run_dir), 'worktree': str(worktree), 'exit_code': code})
            write_json(output / 'benchmark.json', {'commit': commit, 'trials': results, 'acceptance': 'Run jc evaluate for each completed trial; tests are not auto-inferred'})
    return results
