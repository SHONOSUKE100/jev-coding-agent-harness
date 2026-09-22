"""Bounded source retrieval, Python AST symbols, lexical and changed-file hints."""
import ast
import hashlib
import re
import subprocess
from pathlib import Path
from .core import Chunk, SECRET, SOURCE_SUFFIXES

ALIASES = {'認証': ['auth', 'login', 'token'], '期限': ['expire', 'expiry', 'expiration'],
           'データベース': ['database', 'sql', 'repository'], 'ログイン': ['login', 'auth'],
           '画面': ['screen', 'view', 'page'], 'テスト': ['test'], '検索': ['search', 'query']}


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE, timeout=20)


def root_path(repo):
    repo = Path(repo).resolve()
    root = Path(git(repo, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    if root != repo:
        raise ValueError('--repo must point to the Git repository root')
    return root


def terms(task):
    result = set(re.findall(r'[a-zA-Z_][a-zA-Z_0-9]{2,}|[\u3040-\u9fff]{2,}', task.lower()))
    for word, aliases in ALIASES.items():
        if word in task:
            result.update(aliases)
    return result


def source(root, name, max_bytes=100000):
    # macOS /var aliases /private/var; normalize the trusted root before containment checks.
    root = Path(root).resolve()
    path = root / name
    if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(root):
        raise ValueError('Source escapes repository')
    if any((root / Path(*Path(name).parts[:i])).is_symlink() for i in range(1, len(Path(name).parts) + 1)):
        raise ValueError('Symlink source is excluded')
    if not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError('Missing or oversized source')
    text = path.read_text(encoding='utf-8')
    if '\0' in text or SECRET.search(text):
        raise ValueError('Binary or secret-like source excluded')
    return text


def symbols_for(name, text):
    result = []
    if name.endswith('.py'):
        try:
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    result.append({'name': node.name, 'start': min([node.lineno] + [x.lineno for x in node.decorator_list]), 'end': node.end_lineno})
            return sorted(result, key=lambda x: x['start'])
        except (SyntaxError, RecursionError):
            return []
    for i, line in enumerate(text.splitlines(), 1):
        match = re.search(r'\b(?:class|function|func|fn|interface|def)\s+(\w+)', line)
        if match:
            result.append({'name': match[1], 'start': i, 'end': i})
    return result


def retrieve(repo, task, limit=40, expand=False):
    root = root_path(repo)
    names = git(root, 'ls-files', '-z').decode().split('\0')
    changed = set(git(root, 'diff', '--name-only', '-z').decode().split('\0'))
    changed.update(git(root, 'diff', '--cached', '--name-only', '-z').decode().split('\0'))
    words = terms(task)
    candidates, rules, skipped = [], [], []
    for name in names:
        if not name:
            continue
        pinned = Path(name).name in {'AGENTS.md', 'AGENTS.override.md'}
        excluded = any(p.startswith('.') or p in {'node_modules', 'vendor', 'dist', 'build', 'jev-runs'} for p in Path(name).parts)
        if excluded or Path(name).suffix not in SOURCE_SUFFIXES or re.search(r'(^|[/_.-])(secret|credential|password)([/_.-]|$)', name, re.I):
            continue
        try:
            text = source(root, name)
        except (ValueError, UnicodeError, OSError) as exc:
            if pinned:
                raise ValueError(f'Cannot preserve instruction source {name}: {type(exc).__name__}') from exc
            skipped.append(name)
            continue
        lines = text.splitlines(keepends=True)
        spans = []
        # Use complete Python top-level definitions where bounded; other code stays line based.
        if name.endswith('.py') and not pinned:
            try:
                tree = ast.parse(text)
                cursor = 1
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        lo = min([node.lineno] + [d.lineno for d in node.decorator_list])
                        if cursor < lo:
                            spans.append((cursor, lo - 1))
                        spans.append((lo, node.end_lineno))
                        cursor = node.end_lineno + 1
                if cursor <= len(lines):
                    spans.append((cursor, len(lines)))
            except (SyntaxError, RecursionError):
                pass
        if not spans:
            spans = [(1, len(lines))] if lines else []
        for start, end in spans:
            for lo in range(start, end + 1, 80):
                hi = min(lo + 79, end)
                body = ''.join(lines[lo - 1:hi])
                if len(body.encode()) > 12000:
                    if pinned:
                        raise ValueError(f'Instruction chunk too large: {name}')
                    skipped.append(name)
                    continue
                hits = sum(3 * (w in name.lower()) + body.lower().count(w) for w in words)
                hits += 2 * (name in changed)
                id = hashlib.sha256(f'{name}:{lo}:{body}'.encode()).hexdigest()[:16]
                c = Chunk(id, name, lo, hi, body, hits, pinned)
                (rules if pinned else candidates).append(c)
    candidates.sort(key=lambda c: (-c.hits, c.path, c.start))
    matching = [c for c in candidates if c.hits]
    # Include nonmatches in expansion; do not pretend lexical aliases are semantic retrieval.
    selected = (candidates if expand or not matching else matching)[:limit]
    return rules + selected, {'eligible_chunks': len(candidates) + len(rules), 'skipped': len(skipped),
        'skipped_paths': sorted(set(skipped))[:20], 'lexical_match': bool(matching), 'candidate_limit': limit,
        'expanded': expand, 'changed_files': len(changed - {''})}


def inspect_symbols(repo, query, limit=50):
    root = root_path(repo)
    chunks, _ = retrieve(root, query, 200, expand=True)
    result = []
    for name in sorted({c.path for c in chunks if not c.pinned}):
        for item in symbols_for(name, source(root, name)):
            if query.lower() in item['name'].lower():
                result.append({'path': name, **item})
    return result[:limit]
