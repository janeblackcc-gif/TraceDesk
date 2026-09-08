import os
from pathlib import Path
import subprocess
import sys
import pytest
from app.config import Settings, local_ollama_url


def test_dotenv_is_project_local_and_process_environment_wins(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (tmp_path / '.env').write_text('TRACEDESK_PORT=9999\n', encoding='utf-8')
    (project / '.env').write_text('TRACEDESK_PORT=8767\nTRACEDESK_DATA_DIR=storage\n', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    settings = Settings.load(project, {'TRACEDESK_PORT': '8768'})
    assert settings.port == 8768
    assert settings.data_dir == project / 'storage'
    assert Settings.load(project, {}).port == 8767


def test_configuration_does_not_mutate_environment_or_expand_values(tmp_path, monkeypatch):
    monkeypatch.setenv('TRACEDESK_TEST_SENTINEL', 'preserved')
    before = dict(os.environ)
    (tmp_path / '.env').write_text('TRACEDESK_DATA_DIR=${PRIVATE_DIR}\n', encoding='utf-8')
    assert Settings.load(tmp_path, {}).data_dir.name == '${PRIVATE_DIR}'
    assert dict(os.environ) == before


@pytest.mark.parametrize('value', ['0', '65536', 'abc', '', '3.5'])
def test_bad_port_is_rejected(tmp_path, value):
    with pytest.raises(ValueError, match='TRACEDESK_PORT'):
        Settings.load(tmp_path, {'TRACEDESK_PORT': value})


@pytest.mark.parametrize('address', ['https://127.0.0.1:11434', 'http://example.com:11434',
    'http://user@localhost:11434', 'http://localhost:11434/api', 'http://localhost:11434?token=x',
    'http://localhost:11434#fragment', 'http://localhost:65536'])
def test_ollama_url_rejects_remote_and_ambiguous_addresses(address):
    with pytest.raises(ValueError):
        local_ollama_url(address)


def test_config_typo_and_empty_model_are_rejected(tmp_path):
    (tmp_path / '.env').write_text('TRACEDESK_EMBED_MODLE=typo\n', encoding='utf-8')
    with pytest.raises(ValueError, match='TRACEDESK_EMBED_MODLE'):
        Settings.load(tmp_path, {})
    with pytest.raises(ValueError):
        Settings(tmp_path, generation_model='')


def test_launcher_help_does_not_start_a_server():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'scripts/start.py'), '--help'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and '--require-models' in result.stdout and '--check' in result.stdout
