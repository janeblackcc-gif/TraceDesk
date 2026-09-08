"""Launch the local workbench or validate configuration without starting it."""
from __future__ import annotations
import argparse
import json
import re
import socket
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, help='Override TRACEDESK_PORT for this invocation')
    parser.add_argument('--check', action='store_true', help='Print diagnostics as JSON and exit')
    parser.add_argument('--require-models', action='store_true', help='Require the configured local Ollama models')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print('TraceDesk requires Python 3.11+; Python 3.13 is recommended.', file=sys.stderr)
        return 1
    try:
        import uvicorn
        from app import __version__
        from app.config import Settings
        from app.providers import Ollama, ModelUnavailable
    except ImportError as exc:
        print(f'Missing dependency ({exc.name}). Run: python -m pip install -r requirements.txt', file=sys.stderr)
        return 1
    try:
        settings = Settings.load()
        if args.port is not None:
            settings = replace(settings, port=args.port)
        report = {'status': 'passed', 'version': __version__, 'python': sys.version.split()[0],
                  'web_url': f'http://127.0.0.1:{settings.port}', 'port_available': port_available(settings.port),
                  'data_dir': str(settings.data_dir), 'ollama_url': settings.ollama_url,
                  'embedding': settings.embedding_model, 'generation': settings.generation_model,
                  'dependencies': {name: version(name) for name in ('fastapi', 'uvicorn', 'pydantic', 'httpx', 'pypdf', 'python-multipart', 'python-dotenv')},
                  'model_check': {'checked': False}}
        if args.require_models:
            provider = Ollama(settings=settings)
            try:
                state = provider.status()
                report['model_check'] = {'checked': True, **state}
                if state['ready']:
                    runtime_version = provider._request('/api/version').get('version', '')
                    report['model_check']['runtime_version'] = runtime_version
                    match = re.match(r'^(\d+)\.(\d+)\.(\d+)', runtime_version)
                    if not match or tuple(int(part) for part in match.groups()) < (0, 31, 2):
                        report.update(status='failed', error='Ollama 0.31.2+ is required for structured Qwen3.5 replies; 0.33.3 is tested.')
                else:
                    report.update(status='failed', error='Configured local models are unavailable. Start Ollama and install both model tags.')
            finally:
                provider.client.close()
        if args.check or report['status'] != 'passed':
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['status'] == 'passed' else 2
        if not report['port_available']:
            print(f'Port {settings.port} is in use. Open the existing instance or choose another --port.', file=sys.stderr)
            return 1
        print(f"TraceDesk: {report['web_url']}  |  Ctrl+C to stop", flush=True)
        print(f'Data: {settings.data_dir}', flush=True)
        uvicorn.run('app.main:app', host='127.0.0.1', port=settings.port, workers=1, reload=False, access_log=False)
        return 0
    except (ValueError, OSError, ModelUnavailable) as exc:
        print(f'Startup check failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
