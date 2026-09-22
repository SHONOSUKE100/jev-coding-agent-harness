"""Minimal MCP stdio server: newline-delimited JSON-RPC, no shell execution."""
import json
import sys
from pathlib import Path
from .cache import Cache
from .index import inspect_symbols, root_path
from .service import Ranker, prepare, read_chunk, route_tools, state_dir

VERSIONS = {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}


def schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TOOLS = [
    {'name': 'jc_search', 'description': 'Select bounded repository evidence for a task. Calls Jev unless server started offline; use expand to widen candidates.',
     'inputSchema': schema({'query': {'type': 'string', 'minLength': 1, 'maxLength': 4000}, 'expand': {'type': 'boolean'}}, ['query'])},
    {'name': 'jc_read', 'description': 'Recover original source of a selected or dropped chunk from a search in this server session.',
     'inputSchema': schema({'run_id': {'type': 'string'}, 'chunk_id': {'type': 'string'}}, ['run_id', 'chunk_id'])},
    {'name': 'jc_symbol', 'description': 'Search Python AST and other-language declaration names. Does not call Jev.',
     'inputSchema': schema({'query': {'type': 'string', 'minLength': 1, 'maxLength': 500}}, ['query'])},
    {'name': 'jc_tools', 'description': 'Discover relevant schemas from a configured external tool catalog. Does not call or authorize those tools.',
     'inputSchema': schema({'query': {'type': 'string', 'minLength': 1, 'maxLength': 4000}, 'expand': {'type': 'boolean'}}, ['query'])},
]
for tool in TOOLS:
    tool['annotations'] = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': tool['name'] in {'jc_search', 'jc_tools'}}


class Server:
    def __init__(self, repo, config, offline=False):
        self.repo, self.config, self.offline = root_path(repo), config, offline
        self.runs = {}
        self.initialized = False

    def call(self, name, arguments):
        spec = next((x for x in TOOLS if x['name'] == name), None)
        if not spec or not isinstance(arguments, dict):
            raise ValueError('Unknown tool or invalid arguments')
        fields = spec['inputSchema']['properties']
        if set(arguments) - set(fields) or not set(spec['inputSchema']['required']) <= set(arguments):
            raise ValueError('Missing or unknown argument')
        for key, value in arguments.items():
            if fields[key]['type'] == 'boolean':
                if not isinstance(value, bool):
                    raise ValueError('Expected boolean')
            elif not isinstance(value, str) or not value.strip() or len(value) > fields[key].get('maxLength', 200):
                raise ValueError('Invalid string argument')
        if name == 'jc_search':
            out, metrics, rows = prepare(self.repo, arguments['query'], self.config, offline=self.offline, expand=arguments.get('expand', False))
            self.runs[out.name] = out
            return {'run_id': out.name, 'context': (out / 'context-pack.md').read_text(),
                    'candidates': [{'id': r['id'], 'path': r['path'], 'action': r['action']} for r in rows],
                    'pack_tokens_est': metrics['pack_tokens_est'], 'api_failures': metrics['api_failures']}
        if name == 'jc_read':
            out = self.runs.get(arguments['run_id'])
            if out is None:
                raise ValueError('Run ID does not belong to this server session')
            return {'source': read_chunk(out, arguments['chunk_id'])}
        if name == 'jc_symbol':
            return {'symbols': inspect_symbols(self.repo, arguments['query'])}
        cache = None if self.offline else Cache(state_dir(self.repo) / 'cache.sqlite3', self.config.cache_ttl)
        try:
            return route_tools(self.repo, self.config.tools_catalog, arguments['query'], Ranker(self.config, cache, self.offline), arguments.get('expand', False))
        finally:
            if cache:
                cache.close()

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
        if 'id' not in request:
            return None  # Notifications (including initialized/cancelled) never receive replies.
        id = request['id']
        if not isinstance(id, (str, int)) or isinstance(id, bool):
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid id'}}
        method, params = request['method'], request.get('params', {})
        response = {'jsonrpc': '2.0', 'id': id}
        if not isinstance(params, dict):
            return {**response, 'error': {'code': -32602, 'message': 'Invalid params'}}
        if method == 'initialize':
            version = params.get('protocolVersion')
            self.initialized = True
            result = {'protocolVersion': version if isinstance(version, str) and version in VERSIONS else '2025-06-18',
                      'capabilities': {'tools': {'listChanged': False}},
                      'serverInfo': {'name': 'jev-coding-agent-harness', 'version': '0.2.0'}}
        elif method == 'ping':
            result = {}
        elif not self.initialized:
            return {**response, 'error': {'code': -32002, 'message': 'Initialize first'}}
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            try:
                value = self.call(params.get('name'), params.get('arguments', {}))
                result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
            except (ValueError, OSError, KeyError, TypeError) as exc:
                # Do not echo upstream response bodies, credentials or arbitrary invalid input.
                result = {'content': [{'type': 'text', 'text': f'Tool failed ({type(exc).__name__}). Check arguments, config, API key and repository.'}], 'isError': True}
        else:
            return {**response, 'error': {'code': -32601, 'message': 'Method not found'}}
        return {**response, 'result': result}


def serve(repo, config, offline=False, stdin=None, stdout=None):
    server = Server(repo, config, offline)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    while True:
        line = stdin.readline(65537)
        if not line:
            return 0
        if len(line.encode()) > 65536:
            return 1
        try:
            request = json.loads(line)
            response = server.dispatch(request)
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
            stdout.flush()
