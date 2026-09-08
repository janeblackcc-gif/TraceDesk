from pathlib import Path
from scripts.check_ops_dataset import validate
from scripts.release_check import REQUIRED, inspect_files


def release_tree(tmp_path):
    for name in REQUIRED:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')
    return sorted(REQUIRED)


def test_release_accepts_example_configuration_and_bundled_links(tmp_path):
    names = release_tree(tmp_path)
    (tmp_path / '.env.example').write_text('OLLAMA_URL=http://127.0.0.1:11434\n', encoding='utf-8')
    (tmp_path / 'README.md').write_text('[Deploy](docs/deployment.md#install)', encoding='utf-8')
    entries, errors = inspect_files(tmp_path, names)
    assert not errors and len(entries) == len(names)


def test_release_blocks_private_files_excluded_links_and_escape(tmp_path):
    names = release_tree(tmp_path)
    for name in ('.env', 'data/live.db', 'docs/private.md'):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('private', encoding='utf-8')
    (tmp_path / 'README.md').write_text('[Excluded](docs/private.md)\n[Missing](missing.md)', encoding='utf-8')
    _, errors = inspect_files(tmp_path, names + ['.env', 'data/live.db', '../outside.py'])
    assert any('Private or generated' in error and '.env' in error for error in errors)
    assert any('Private or generated' in error and 'live.db' in error for error in errors)
    assert any('Unsafe source' in error for error in errors)
    assert any('excluded file' in error for error in errors)
    assert any('Broken or external' in error for error in errors)


def test_release_detects_machine_path_and_credentials(tmp_path):
    names = release_tree(tmp_path)
    for value in ('Z' + ':/private/owner', 'gh' + 'p_' + 'a' * 24):
        (tmp_path / 'README.md').write_text(value, encoding='utf-8')
        assert any('credential-like' in error for error in inspect_files(tmp_path, names)[1])


def test_dataset_preflight_does_not_modify_frozen_files():
    directory = Path(__file__).resolve().parents[1] / 'datasets/tracedesk_ops'
    before = {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    report = validate(directory)
    assert report['status'] == 'passed' and report['total_chunks'] == 17
    assert before == {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
