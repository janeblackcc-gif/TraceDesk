"""Check the public source set and optionally build an archive with file hashes."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import __version__

REQUIRED = {'.gitignore', '.env.example', 'README.md', 'LICENSE', 'requirements.txt', 'requirements-dev.txt',
            'app/main.py', 'scripts/start.py', 'docs/deployment.md', 'docs/architecture.md', 'docs/evaluation.md',
            '.github/workflows/tests.yml', 'tests/test_core.py', 'demo/manifest.json'}
ROOT_FILES = {'.gitignore', '.gitattributes', '.env.example', 'README.md', 'LICENSE', 'CHANGELOG.md',
              'CONTRIBUTING.md', 'requirements.txt', 'requirements-dev.txt', 'start_local_rag.ps1'}
ROOT_DIRS = {'.github', 'app', 'web', 'tests', 'demo', 'eval', 'datasets', 'scripts', 'docs'}
PRIVATE_NAMES = {'data', '.venv', '.git', 'evidence', '.release-work', '__pycache__', '.pytest_cache'}
TEXT_SUFFIXES = {'.py', '.md', '.json', '.jsonl', '.txt', '.toml', '.yml', '.yaml', '.ps1', '.js', '.css', '.html'}
PRIVATE_TEXT = re.compile(r'(?<![\w])[A-Za-z]:[\\/][^\s"<>]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}')
MARKDOWN_LINK = re.compile(r'\]\(([^)]+)\)')


def source_files(root: Path) -> list[str]:
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            capture_output=True, check=True, timeout=30)
    return sorted(set(name for name in result.stdout.decode('utf-8').split('\0') if name))


def inspect_files(root: Path, names: list[str]) -> tuple[list[dict], list[str]]:
    entries, errors = [], []
    for missing in sorted(REQUIRED - set(names)):
        errors.append(f'Missing release file: {missing}')
    for name in names:
        relative = Path(name)
        path = root / relative
        if relative.is_absolute() or not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            errors.append(f'Unsafe source path: {name}')
            continue
        if (len(relative.parts) == 1 and name not in ROOT_FILES) or (len(relative.parts) > 1 and relative.parts[0] not in ROOT_DIRS):
            errors.append(f'Unreviewed top-level path: {name}')
        if (set(relative.parts) & PRIVATE_NAMES or
                (relative.name.startswith('.env') and relative.name != '.env.example') or
                relative.suffix.lower() in {'.log', '.db', '.sqlite', '.sqlite3', '.gguf', '.safetensors', '.zip'} or
                relative.name.endswith(('.db-wal', '.db-shm'))):
            errors.append(f'Private or generated file included: {name}')
            continue
        if not path.is_file():
            errors.append(f'Source file unavailable: {name}')
            continue
        raw = path.read_bytes()
        if len(raw) > 5 * 1024 * 1024:
            errors.append(f'File exceeds 5 MiB review threshold: {name}')
        if relative.suffix.lower() in TEXT_SUFFIXES or name == '.env.example':
            text = raw.decode('utf-8-sig')
            if PRIVATE_TEXT.search(text):
                errors.append(f'Absolute machine path or credential-like text: {name}')
            if relative.suffix.lower() == '.md':
                for match in MARKDOWN_LINK.finditer(text):
                    target = urlsplit(match.group(1).strip('<>'))
                    if target.scheme or not target.path:
                        continue
                    linked = (root / unquote(target.path.lstrip('/')) if target.path.startswith('/')
                              else path.parent / unquote(target.path)).resolve()
                    if not linked.is_relative_to(root.resolve()) or not linked.exists():
                        errors.append(f'Broken or external local link in {name}: {target.path}')
                    elif linked.is_file() and linked.relative_to(root.resolve()).as_posix() not in names:
                        errors.append(f'Link points to an excluded file in {name}: {target.path}')
        entries.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    return entries, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/release_check.json')
    parser.add_argument('--archive', type=Path, help='Write a new zip after all checks pass; never overwrite')
    args = parser.parse_args()
    entries, errors = inspect_files(ROOT, source_files(ROOT))
    report = {'status': 'failed' if errors else 'passed', 'version': __version__,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'files': entries, 'errors': errors,
              'notice': 'Static release-content and link checks; does not certify runtime quality or external security.'}
    if args.archive and not errors:
        args.archive.parent.mkdir(parents=True, exist_ok=True)
        prefix = f'TraceDesk-{__version__}'
        with zipfile.ZipFile(args.archive, 'x', zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                raw = (ROOT / entry['path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                    raise RuntimeError(f"Source changed while building: {entry['path']}")
                archive.writestr(f"{prefix}/{entry['path']}", raw)
            archive.writestr(f'{prefix}/RELEASE_MANIFEST.json', json.dumps(report, ensure_ascii=False, indent=2))
        with zipfile.ZipFile(args.archive) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('Archive CRC verification failed')
        report['archive'] = {'name': args.archive.name, 'bytes': args.archive.stat().st_size,
                             'sha256': hashlib.sha256(args.archive.read_bytes()).hexdigest()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'version': __version__, 'file_count': len(entries),
                      'errors': errors, 'archive': report.get('archive')}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
