"""Compare the Team API with its reviewable frozen schema; never connects to a DB."""
import argparse
import difflib
import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.application import create_application
from app.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Update the reviewed schema after intentional API changes')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tracedesk-openapi-') as temporary:
        url = URL.create('postgresql+psycopg', username='schema', host='127.0.0.1', database='schema')
        app = create_application(Settings(data_dir=Path(temporary), database_url=url.render_as_string(hide_password=False)))
        try:
            actual = json.dumps(app.openapi(), ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        finally:
            app.state.telemetry.shutdown()
            app.state.database.close()
    target = ROOT / 'docs/industrial/openapi.json'
    if args.write:
        target.write_text(actual, encoding='utf-8')
        print('OpenAPI snapshot written; inspect the schema diff before release')
        return 0
    expected = target.read_text(encoding='utf-8')
    if actual != expected:
        print(''.join(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True),
                                         fromfile='reviewed', tofile='current')))
        return 1
    print('OpenAPI matches the reviewed snapshot')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
