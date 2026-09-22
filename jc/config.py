"""Validated, data-only project configuration. Never controls shell commands."""
from dataclasses import dataclass, fields
from pathlib import Path
import math
import tomllib


@dataclass(frozen=True)
class Config:
    budget: int = 8000
    candidates: int = 40
    full_threshold: float = .8
    excerpt_threshold: float = .4
    model: str = 'jev-latest'
    timeout: float = 20
    retries: int = 1
    cache_ttl: int = 86400
    rules_manifest: str = ''
    tools_catalog: str = ''
    tool_limit: int = 5
    small_model: str = ''
    large_model: str = ''
    routing_threshold: float = .95

    def validate(self):
        bounds = {'budget': (256, 200000), 'candidates': (1, 200), 'timeout': (.1, 120),
                  'retries': (0, 3), 'cache_ttl': (0, 2592000), 'tool_limit': (1, 50),
                  'full_threshold': (0, 1), 'excerpt_threshold': (0, 1), 'routing_threshold': (.5, 1)}
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            default = getattr(Config(), name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
                raise ValueError(f'Invalid config {name}: expected {lo}–{hi}')
            if isinstance(default, int) and not isinstance(value, int):
                raise ValueError(f'{name} must be an integer')
        if self.excerpt_threshold > self.full_threshold:
            raise ValueError('excerpt_threshold must not exceed full_threshold')
        for name in ('model', 'rules_manifest', 'tools_catalog', 'small_model', 'large_model'):
            if not isinstance(getattr(self, name), str) or len(getattr(self, name)) > 1000:
                raise ValueError(f'{name} must be a bounded string')
        if not self.model:
            raise ValueError('model cannot be empty')
        return self


def load_config(repo: Path, explicit: str | None = None, **overrides) -> Config:
    path = Path(explicit).resolve() if explicit else repo / '.jev/config.toml'
    data = {}
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 64000:
            raise ValueError('Unsafe or oversized config')
        data = tomllib.loads(path.read_text())
        if set(data) - {f.name for f in fields(Config)}:
            raise ValueError('Unknown config keys: ' + ', '.join(sorted(set(data) - {f.name for f in fields(Config)})))
    elif explicit:
        raise ValueError('Config not found')
    data.update({k: v for k, v in overrides.items() if v is not None})
    return Config(**data).validate()
