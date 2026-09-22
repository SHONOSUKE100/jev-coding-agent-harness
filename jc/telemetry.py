"""Codex JSONL event aggregation; completion is not task acceptance."""
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from .core import estimate
from .service import read_json, write_json


class Observer:
    def __init__(self):
        self.usage = Counter()
        self.usage_turns = 0
        self.usage_field_turns = Counter()
        self.completed_turns = 0
        self.failed = False
        self.tool_calls = 0
        self.read_search_calls = 0
        self.read_search_output_est = 0
        self.last_message = ''
        self.seen_items = set()
        self.repeats = Counter()
        self.loop_warnings = []
        self.parse_errors = 0

    def feed(self, event):
        if not isinstance(event, dict):
            self.parse_errors += 1
            return ''
        kind = event.get('type', '')
        if kind in {'turn.failed', 'error'}:
            self.failed = True
        if kind == 'turn.completed':
            self.completed_turns += 1
            usage = event.get('usage', {})
            if isinstance(usage, dict) and all(isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')):
                self.usage_turns += 1
                for k in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'):
                    value = usage.get(k)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        self.usage[k] += value
                        self.usage_field_turns[k] += 1
        if kind != 'item.completed':
            return kind
        item = event.get('item', {})
        if not isinstance(item, dict):
            return kind
        id = item.get('id')
        if id and id in self.seen_items:
            return ''
        if id:
            self.seen_items.add(id)
        if item.get('type') == 'agent_message':
            self.last_message = str(item.get('text', ''))
        if item.get('type') in {'command_execution', 'mcp_tool_call', 'web_search'}:
            self.tool_calls += 1
            command = str(item.get('command', item.get('tool', item.get('type'))))
            output = str(item.get('aggregated_output', item.get('result', '')))
            is_search = bool(re.search(r'\b(rg|grep|cat|sed|head|tail|find|jc[-_ ](?:search|read|symbol))\b', command))
            if is_search:
                self.read_search_calls += 1
                self.read_search_output_est += estimate(output)
            fingerprint = hashlib.sha256((command + '\n' + output).encode()).hexdigest()
            self.repeats[fingerprint] += 1
            if self.repeats[fingerprint] == 3:
                self.loop_warnings.append({'command': command[:200], 'identical_result_count': 3})
            return 'tool: ' + command[:100]
        return str(item.get('type', kind))

    def metrics(self):
        known = self.usage_turns > 0 and self.usage_turns == self.completed_turns
        return {'codex_input_tokens': self.usage['input_tokens'] if known else None,
                'codex_output_tokens': self.usage['output_tokens'] if known else None,
                'codex_cached_input_tokens': self.usage['cached_input_tokens'] if known and self.usage_field_turns['cached_input_tokens'] == self.completed_turns else None,
                'usage_complete': known, 'observed_usage': dict(self.usage),
                'completed_turns': self.completed_turns, 'tool_calls': self.tool_calls,
                'read_search_calls_heuristic': self.read_search_calls,
                'read_search_output_tokens_est': self.read_search_output_est,
                'loop_warnings': self.loop_warnings, 'event_parse_errors': self.parse_errors}


def summarize_logs(text, max_lines=30):
    patterns = {'dependency': r'ModuleNotFound|ImportError|Cannot find module|missing dependency',
                'database': r'SQLSTATE|OperationalError|relation .* does not exist|database.*(?:error|timeout)',
                'network': r'ECONNRESET|ETIMEDOUT|ConnectionError|connection refused|DNS',
                'type_error': r'TypeError|type mismatch|TS\d{4}',
                'test_assertion': r'AssertionError|FAILED|assert .*=='}
    counts, selected = Counter(), []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        categories = [name for name, pat in patterns.items() if re.search(pat, line, re.I)]
        counts.update(categories)
        if categories:
            selected.extend(range(max(0, i - 1), min(len(lines), i + 2)))
    indexes = sorted(set(selected))[:max_lines]
    if not indexes:
        indexes = list(range(max(0, len(lines) - max_lines), len(lines)))
    return {'category': counts.most_common(1)[0][0] if counts else 'unknown',
            'method': 'deterministic-patterns', 'counts': dict(counts),
            'lines': [{'line': i + 1, 'text': lines[i][:2000]} for i in indexes],
            'original_lines': len(lines), 'truncated': len(set(selected)) > max_lines}


def evaluate(run_dir, success, note):
    path = Path(run_dir) / 'metrics.json'
    metrics = read_json(path)
    if metrics.get('codex_status') != 'completed' and success:
        raise ValueError('Cannot accept an incomplete or failed Codex execution')
    metrics.update(task_success=success, evaluation={'method': 'human', 'note': note})
    write_json(path, metrics)
    return metrics


def compare(baseline_dirs, harness_dirs, prices=None):
    def group(dirs):
        groups = {}
        for directory in dirs:
            m = read_json(Path(directory) / 'metrics.json')
            key = (m.get('task_id'), m.get('commit'), m.get('snapshot'), m.get('codex_model'), m.get('sandbox'))
            if not all(key[:3]):
                raise ValueError('Comparison needs v2 executed runs with task/commit/snapshot')
            if key in groups:
                raise ValueError('Duplicate task/snapshot; compare one trial per pair')
            groups[key] = m
        return groups
    baseline, harness = group(baseline_dirs), group(harness_dirs)
    if not baseline or set(baseline) != set(harness):
        raise ValueError('Pair identical task, commit, working diff, model and sandbox across both groups')
    for m in baseline.values():
        if m.get('mode') != 'baseline':
            raise ValueError('Baseline group contains a harness run')
    for m in harness.values():
        if m.get('mode') != 'harness':
            raise ValueError('Harness group contains a baseline run')
    def aggregate(group):
        values = list(group.values())
        complete = all(isinstance(m.get('codex_input_tokens'), int) for m in values)
        accepted = [m for m in values if m.get('task_success') is True]
        reviewed = all(isinstance(m.get('task_success'), bool) for m in values)
        total = sum(m['codex_input_tokens'] for m in values) if complete else None
        return {'runs': len(values), 'reviewed': reviewed, 'successes': len(accepted), 'input_tokens': total,
                'success_rate': len(accepted) / len(values) if reviewed else None,
                # All failed/retried task cost contributes to cost per accepted task.
                'input_tokens_per_success': total / len(accepted) if complete and reviewed and accepted else None,
                'wall_seconds': sum(m.get('codex_seconds', 0) + m.get('elapsed_seconds', 0) for m in values),
                'cost_usd': total_cost(values, prices) if prices is not None else None,
                'jev_calls': sum(m.get('api_calls', 0) for m in values)}
    a, b = aggregate(baseline), aggregate(harness)
    return {'baseline': a, 'harness': b, 'input_reduction': 1 - b['input_tokens'] / a['input_tokens'] if a['input_tokens'] and b['input_tokens'] is not None else None,
            'note': 'Same-snapshot paired comparison; source-pack estimates are not billed usage. Pricing is not assumed.'}


def total_cost(metrics_list, prices):
    """User-supplied USD / million rates. Missing usage remains unknown."""
    import math
    required = {'codex_input', 'codex_cached_input', 'codex_output', 'jev_input', 'jev_output'}
    if not isinstance(prices, dict) or set(prices) != required:
        raise ValueError('Prices require exactly codex_input, codex_cached_input, codex_output, jev_input, jev_output')
    for value in prices.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Prices must be finite, nonnegative USD per million tokens')
    total = 0
    for m in metrics_list:
        counts = [m.get(k) for k in ('codex_input_tokens', 'codex_cached_input_tokens', 'codex_output_tokens')]
        if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in counts):
            return None
        incoming, cached, outgoing = counts
        if cached > incoming or m.get('api_failures', 0) or m.get('child_usage_incomplete'):
            return None
        total += ((incoming - cached) * prices['codex_input'] + cached * prices['codex_cached_input'] + outgoing * prices['codex_output']) / 1_000_000
        batches = m.get('jev_reported_usage_by_batch', [])
        if len(batches) != m.get('api_calls', 0):
            return None
        for usage in batches:
            if not isinstance(usage, dict) or any(not isinstance(usage.get(k), int) or isinstance(usage.get(k), bool) or usage[k] < 0 for k in ('input_tokens', 'output_tokens')):
                return None
            total += (usage['input_tokens'] * prices['jev_input'] + usage['output_tokens'] * prices['jev_output']) / 1_000_000
    return total
