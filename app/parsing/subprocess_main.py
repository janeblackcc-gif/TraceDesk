"""Runs only inside the networkless parser container; no DB/model credentials."""
import json
import logging
import resource
import signal
import sys
from pathlib import Path
from importlib.metadata import version

from app.ingest import InputError, MAX_BYTES, chunks, parse

resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
logging.disable(logging.CRITICAL)
signal.signal(signal.SIGALRM, lambda signum, frame: sys.exit(4))
signal.alarm(120)


def main() -> int:
    try:
        with Path('/input/source').open('rb') as source:
            data = source.read(MAX_BYTES + 1)
        name, pages = parse(sys.argv[1], data)
        result = {'filename': name, 'pages': pages, 'chunks': chunks(pages), 'parser_version': version('pypdf')}
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))
        return 0
    except InputError:
        print(json.dumps({'error': 'INPUT_INVALID'}))
        return 2
    except Exception:
        print(json.dumps({'error': 'PARSER_FAILED'}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
