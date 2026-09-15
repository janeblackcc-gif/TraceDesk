import os
from pathlib import Path
import subprocess
import sys
import pytest
from app.config import Settings, local_ollama_url, local_vllm_url


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


@pytest.mark.parametrize('address', ['https://127.0.0.1:8000', 'http://example.com:8000',
    'http://user@localhost:8000', 'http://localhost:8000/v1', 'http://localhost:8000?token=x',
    'http://localhost:8000#fragment', 'http://localhost:65536'])
def test_vllm_url_rejects_remote_and_ambiguous_addresses(address):
    with pytest.raises(ValueError):
        local_vllm_url(address)


def test_vllm_configuration_requires_pinned_model_digests(tmp_path):
    with pytest.raises(ValueError, match='requires TRACEDESK_EMBED_MODEL_DIGEST'):
        Settings.load(tmp_path, {'TRACEDESK_MODEL_PROVIDER': 'vllm'})
    settings = Settings.load(tmp_path, {
        'TRACEDESK_MODEL_PROVIDER': 'vllm',
        'TRACEDESK_EMBED_MODEL_DIGEST': 'a' * 64,
        'TRACEDESK_CHAT_MODEL_DIGEST': 'b' * 64,
    })
    assert settings.embedding_model == 'Qwen/Qwen3-Embedding-0.6B'
    assert settings.generation_model == 'Qwen/Qwen3-4B-Instruct-2507'
    assert settings.vllm_embed_url == 'http://127.0.0.1:8001'
    assert settings.vllm_chat_url == 'http://127.0.0.1:8000'


def test_vllm_api_key_file_is_private(tmp_path):
    secret = tmp_path / 'vllm-key'
    secret.write_text('synthetic-vllm-secret\n', encoding='utf-8')
    settings = Settings.load(tmp_path, {
        'TRACEDESK_MODEL_PROVIDER': 'vllm',
        'TRACEDESK_EMBED_MODEL_DIGEST': 'a' * 64,
        'TRACEDESK_CHAT_MODEL_DIGEST': 'b' * 64,
        'TRACEDESK_VLLM_API_KEY_FILE': str(secret),
    })
    assert settings.vllm_api_key == 'synthetic-vllm-secret'
    assert 'synthetic-vllm-secret' not in repr(settings)


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


def test_environment_typos_and_secret_file_precedence(tmp_path):
    with pytest.raises(ValueError, match='TRACEDESK_EMBED_MODLE'):
        Settings.load(tmp_path, {'TRACEDESK_EMBED_MODLE': 'wrong'})
    secret = tmp_path / 'secret'
    secret.write_text('postgresql+psycopg://fixture:fixture@localhost/fixture\n', encoding='utf-8')
    config = Settings.load(tmp_path, {'DATABASE_URL_FILE': str(secret)})
    assert config.database_url.endswith('/fixture')
    assert 'fixture:fixture' not in repr(config)
    with pytest.raises(ValueError, match='Configure only one'):
        Settings.load(tmp_path, {'DATABASE_URL_FILE': str(secret), 'DATABASE_URL': 'ambiguous'})
    with pytest.raises(ValueError, match='must reference'):
        Settings.load(tmp_path, {'DATABASE_URL_FILE': str(tmp_path / 'missing')})
    with pytest.raises(ValueError, match='Production requires'):
        Settings.load(tmp_path, {'DATABASE_URL_FILE': str(secret), 'TRACEDESK_DEPLOYMENT': 'production'})
