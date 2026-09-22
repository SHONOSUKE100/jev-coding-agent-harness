"""Reusable selection pipeline shared by CLI, runner and MCP."""
import hashlib
import json
import os
import time
import tomllib
import urllib.error
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from .cache import Cache
from .config import Config
from .core import Chunk, ask, estimate, request_body, save_run, select, validate_answers
from .index import git, retrieve, root_path, source


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def atomic_write(path: Path, text: str):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    if path.stat().st_size > 20000000:
        raise ValueError('Oversized JSON')
    return json.loads(path.read_text())


def state_dir(repo):
    # Git metadata is not candidate source, and is not committed to the target repo.
    raw = Path(git(repo, 'rev-parse', '--git-path', 'jev-harness').decode().strip())
    directory = raw if raw.is_absolute() else repo / raw
    if directory.is_symlink():
        raise ValueError('State directory cannot be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory.resolve()


class Ranker:
    def __init__(self, config, cache=None, offline=False, progress=lambda message: None):
        self.config, self.cache, self.offline, self.progress = config, cache, offline, progress
        self.stats = {'api_calls': 0, 'api_failures': 0, 'cache_hits': 0, 'cache_misses': 0,
                      'jev_reported_usage_by_batch': [], 'rank_mode': 'offline' if offline else 'jev'}

    def rank(self, task, chunks, criterion=None):
        scores, fallback = {}, set()
        pending = [c for c in chunks if not c.pinned]
        if self.offline:
            return scores, {c.id for c in pending}
        key = os.environ.get('TYPESAFE_API_KEY', '')
        for offset in range(0, len(pending), 3):
            batch = pending[offset:offset + 3]
            body = request_body(task, batch, self.config.model, criterion)
            cached = self.cache.get(body) if self.cache else None
            if cached is not None:
                try:
                    validated = validate_answers({'answers': {k: {'noul': v} for k, v in cached.items()}}, batch)
                    scores.update(validated)
                    self.stats['cache_hits'] += len(batch)
                    continue
                except (ValueError, AttributeError):
                    pass
            self.stats['cache_misses'] += len(batch)
            if not key:
                raise ValueError('TYPESAFE_API_KEY is required for uncached judgments; use --offline explicitly')
            for attempt in range(self.config.retries + 1):
                self.stats['api_calls'] += 1
                self.progress(f'Jev batch {offset // 3 + 1}, attempt {attempt + 1}')
                try:
                    judged, usage = ask(task, batch, key, self.config.model, self.config.timeout, criterion)
                    scores.update(judged)
                    self.stats['jev_reported_usage_by_batch'].append(usage)
                    if self.cache:
                        self.cache.put(body, judged)
                    break
                except (OSError, ValueError, TypeError) as exc:
                    self.stats['api_failures'] += 1
                    retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code == 429 or exc.code >= 500
                    if attempt < self.config.retries and retryable:
                        time.sleep(min(.25 * 2 ** attempt, 1))
                        continue
                    fallback.update(c.id for c in batch)
                    self.progress(f'Jev fallback: {type(exc).__name__}; original candidates retained within budget')
                    break
        return scores, fallback


def bounded_file(repo, relative, max_bytes=100000):
    if not relative:
        raise ValueError('File path is empty')
    return source(repo, relative, max_bytes)


def route_rules(repo, manifest, task, ranker):
    if not manifest:
        return '', []
    data = tomllib.loads(bounded_file(repo, manifest))
    entries = data.get('rules', [])
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError('rules manifest must contain at most 100 rules')
    chunks, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'path', 'optional'} or not isinstance(entry.get('path'), str):
            raise ValueError('Each rule requires path and optional boolean')
        name = entry['path']
        optional = entry.get('optional', False)
        if not isinstance(optional, bool) or name in seen:
            raise ValueError('Invalid or duplicate rule entry')
        if Path(name).name.startswith('AGENTS'):
            raise ValueError('AGENTS.md is managed by Codex, not the optional-rule router')
        seen.add(name)
        text = bounded_file(repo, name, 12000)
        id = hashlib.sha256((name + text).encode()).hexdigest()[:16]
        chunks.append(Chunk(id, name, 1, max(1, len(text.splitlines())), text, pinned=not optional))
    scores, fallback = ranker.rank(task, chunks, 'Does this supplementary guidance apply to the task?')
    selected, rows = [], []
    for c in chunks:
        keep = c.pinned or c.id in fallback or scores.get(c.id, 1) >= ranker.config.excerpt_threshold
        rows.append({'path': c.path, 'required': c.pinned, 'selected': keep, 'score': scores.get(c.id)})
        if keep:
            selected.append(c.block())
    return '\n'.join(selected), rows


def route_tools(repo, catalog, task, ranker, expand=False):
    if not catalog:
        return {'tools': [], 'decisions': []}
    data = json.loads(bounded_file(repo, catalog, 200000))
    if not isinstance(data, list) or len(data) > 100:
        raise ValueError('Tool catalog must be a list of at most 100 tools')
    chunks, by_id = [], {}
    for tool in data:
        if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not isinstance(tool.get('description'), str) or not isinstance(tool.get('inputSchema'), dict):
            raise ValueError('Tool requires name, description and inputSchema')
        if len(tool['name']) > 128 or len(tool['description']) > 2000 or tool['name'] in by_id:
            raise ValueError('Duplicate or oversized tool metadata')
        by_id[tool['name']] = tool
        chunks.append(Chunk(tool['name'], tool['name'], 1, 1, tool['description']))
    if expand:
        return {'tools': data, 'decisions': [], 'expanded': True}
    scores, fallback = ranker.rank(task, chunks, 'Would this tool help complete the task?')
    ordered = sorted(chunks, key=lambda c: (-scores.get(c.id, 1), c.id))
    kept = [c.id for c in ordered if c.id in fallback or scores.get(c.id, 1) >= ranker.config.excerpt_threshold][:ranker.config.tool_limit]
    return {'tools': [by_id[id] for id in kept], 'decisions': [{'name': c.id, 'score': scores.get(c.id), 'selected': c.id in kept} for c in ordered]}


def choose_model(task, config, ranker):
    if not config.small_model or not config.large_model:
        raise ValueError('--auto-model requires small_model and large_model in config')
    c = Chunk('simple-task', 'model-routing', 1, 1,
              'The task is a small, mechanical, localized edit or lookup; it needs no architecture, security judgment, ambiguous requirements, or novel debugging.')
    scores, fallback = ranker.rank(task, [c], 'Does the task satisfy ALL simplicity conditions stated in this item?')
    score = scores.get(c.id)
    simple = c.id not in fallback and score is not None and score >= config.routing_threshold
    return {'model': config.small_model if simple else config.large_model, 'score': score,
            'reason': 'simple-task-threshold' if simple else 'conservative-default'}


def prepare(repo, task, config: Config, out=None, offline=False, no_cache=False, expand=False, progress=lambda msg: None):
    started = time.monotonic()
    if not isinstance(task, str) or not task.strip() or len(task.encode()) > 8000:
        raise ValueError('Task must contain 1–8000 UTF-8 bytes')
    config.validate()
    repo = root_path(repo)
    state = state_dir(repo)
    out = Path(out).resolve() if out else state / 'runs' / uuid4().hex[:12]
    if out.exists():
        raise ValueError('Output already exists; choose a new directory')
    cache = None if no_cache or offline else Cache(state / 'cache.sqlite3', config.cache_ttl)
    ranker = Ranker(config, cache, offline, progress)
    try:
        chunks, retrieval = retrieve(repo, task, config.candidates, expand)
        if not chunks:
            raise ValueError('No eligible tracked sources; use baseline or add source files to Git')
        scores, fallback = ranker.rank(task, chunks)
        guidance, rule_rows = route_rules(repo, config.rules_manifest, task, ranker)
        tools = route_tools(repo, config.tools_catalog, task, ranker)
        auxiliary = ''
        if guidance:
            auxiliary += '\n# Supplementary guidance (repository instructions take precedence)\n' + guidance
        if tools['tools']:
            auxiliary += '\n# Suggested external tool metadata (discovery only; not execution)\n' + json.dumps(
                [{'name': t['name'], 'description': t['description']} for t in tools['tools']], ensure_ascii=False)
        budget = config.budget - estimate(auxiliary) - 2
        if budget < 256:
            raise ValueError('Required guidance exhausts context budget; increase budget')
        pack, rows = select(chunks, scores, budget, task, fallback, config.full_threshold, config.excerpt_threshold)
        pack += auxiliary
        if estimate(pack) > config.budget:
            raise ValueError('Combined context exceeds budget')
        if offline:
            for row in rows:
                if row['reason'] == 'api-fallback':
                    row['reason'] = 'offline-retain'
        before, after = (sum(r[k] for r in rows) for k in ('before_est', 'after_est'))
        commit = git(repo, 'rev-parse', 'HEAD').decode().strip()
        snapshot = hashlib.sha256(git(repo, 'diff', 'HEAD', '--') + git(repo, 'status', '--porcelain')).hexdigest()
        metrics = {'schema_version': 2, 'mode': 'harness', 'task': task, 'task_id': hashlib.sha256(task.encode()).hexdigest(),
                   'repository': str(repo), 'commit': commit, 'snapshot': snapshot, 'config': asdict(config),
                   'source_before_est': before, 'source_after_est': after,
                   'source_reduction_pct': (1 - after / before) * 100 if before else 0,
                   'pack_tokens_est': estimate(pack), 'budget_tokens_est': config.budget,
                   'estimator': 'ceil(UTF-8 bytes / 3); not a tokenizer', **ranker.stats,
                   'elapsed_seconds': round(time.monotonic() - started, 4),
                   'codex_input_tokens': None, 'task_success': None, 'retrieval': retrieval}
        save_run(out, task, chunks, pack, rows, metrics)
        write_json(out / 'rules.json', rule_rows)
        atomic_write(out / 'current-rules.md', guidance)
        write_json(out / 'tools.json', tools)
        record_child_search(repo, out, metrics)
        return out, metrics, rows
    finally:
        if cache:
            cache.close()


def read_chunk(run_dir, chunk_id):
    chunks = read_json(Path(run_dir) / 'chunks.json')
    c = next((c for c in chunks if c['id'] == chunk_id), None)
    if c is None:
        raise ValueError('Unknown chunk ID')
    return Chunk(**c).block()


def record_child_search(repo, out, metrics):
    parent_value = os.environ.get('JEV_PARENT_RUN_DIR')
    if not parent_value:
        return
    parent = Path(parent_value)
    if parent.resolve() == out.resolve() or parent.is_symlink():
        return
    try:
        parent_metrics = read_json(parent / 'metrics.json')
        if parent_metrics.get('repository') != str(repo):
            return
        event = {'run_dir': str(out), **{k: metrics.get(k) for k in
                 ('api_calls', 'api_failures', 'cache_hits', 'cache_misses', 'jev_reported_usage_by_batch')}}
        path = parent / 'child-searches.jsonl'
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, (json.dumps(event) + '\n').encode())
        finally:
            os.close(fd)
    except (OSError, ValueError):
        # Linking is telemetry only and must not prevent evidence retrieval.
        pass
