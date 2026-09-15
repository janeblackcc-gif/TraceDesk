"""Project-local configuration with explicit precedence and validation."""
from __future__ import annotations
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
KEYS = {'TRACEDESK_DATA_DIR', 'TRACEDESK_PORT', 'TRACEDESK_MODEL_PROVIDER', 'TRACEDESK_OLLAMA_URL',
        'TRACEDESK_VLLM_CHAT_URL', 'TRACEDESK_VLLM_EMBED_URL', 'TRACEDESK_VLLM_API_KEY',
        'TRACEDESK_VLLM_API_KEY_FILE', 'TRACEDESK_EMBED_MODEL', 'TRACEDESK_CHAT_MODEL',
        'TRACEDESK_EMBED_MODEL_DIGEST', 'TRACEDESK_CHAT_MODEL_DIGEST', 'TRACEDESK_PUBLIC_ORIGIN',
        'TRACEDESK_BOOTSTRAP_TOKEN', 'TRACEDESK_BOOTSTRAP_TOKEN_FILE', 'TRACEDESK_DEPLOYMENT'}
TEST_KEYS = {'TRACEDESK_TEST_DATABASE_URL', 'TRACEDESK_PARSER_TEST_IMAGE', 'TRACEDESK_BACKUP_TEST_CONTAINER',
             'TRACEDESK_TEST_ARTIFACT_DIR'}


def _local_model_url(value: str, provider: str) -> str:
    value = value.strip().rstrip('/')
    parsed = urlparse(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or
            parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError(f'{provider} 地址仅允许本机 HTTP 地址，不支持远程、凭据、路径或查询参数。')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f'{provider} 端口必须在 1 至 65535 之间。') from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f'{provider} 端口必须在 1 至 65535 之间。')
    return value


def local_ollama_url(value: str) -> str:
    return _local_model_url(value, 'Ollama')


def local_vllm_url(value: str) -> str:
    return _local_model_url(value, 'vLLM')


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8765
    ollama_url: str = 'http://127.0.0.1:11434'
    embedding_model: str = 'qwen3-embedding:0.6b-q8_0'
    generation_model: str = 'qwen3.5:4b-q4_K_M'
    database_url: str | None = field(default=None, repr=False)
    public_origin: str = 'https://localhost'
    bootstrap_token: str | None = field(default=None, repr=False)
    deployment: str = 'development'
    model_provider: str = 'ollama'
    vllm_chat_url: str = 'http://127.0.0.1:8000'
    vllm_embed_url: str = 'http://127.0.0.1:8001'
    embedding_model_digest: str | None = None
    generation_model_digest: str | None = None
    vllm_api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.deployment not in {'development', 'production'}:
            raise ValueError('TRACEDESK_DEPLOYMENT must be development or production')
        if self.model_provider not in {'ollama', 'vllm'}:
            raise ValueError('TRACEDESK_MODEL_PROVIDER must be ollama or vllm')
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('TRACEDESK_PORT 必须是 1 至 65535 之间的整数。')
        local_ollama_url(self.ollama_url)
        local_vllm_url(self.vllm_chat_url)
        local_vllm_url(self.vllm_embed_url)
        if self.database_url is not None:
            from .db.session import validate_database_url
            validate_database_url(self.database_url)
            origin = urlparse(self.public_origin)
            if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or
                    origin.path or origin.query or origin.fragment):
                raise ValueError('TRACEDESK_PUBLIC_ORIGIN must be an HTTPS origin without a path')
            if self.bootstrap_token is not None and len(self.bootstrap_token) < 32:
                raise ValueError('TRACEDESK_BOOTSTRAP_TOKEN must contain at least 32 characters')
            if self.deployment == 'production':
                url = validate_database_url(self.database_url)
                if not url.password or len(url.password) < 24 or origin.hostname in {'localhost', '127.0.0.1', '::1'}:
                    raise ValueError('Production requires a strong database credential and a real HTTPS origin')
        elif self.deployment == 'production':
            raise ValueError('Production requires DATABASE_URL')
        for name in (self.embedding_model, self.generation_model):
            if not name or any(character.isspace() for character in name):
                raise ValueError('模型标签不能为空或包含空白字符。')
            if 'cloud' in name.lower():
                raise ValueError('请使用本地模型标签，不支持 cloud 模型。')
        for name, digest in (('TRACEDESK_EMBED_MODEL_DIGEST', self.embedding_model_digest),
                             ('TRACEDESK_CHAT_MODEL_DIGEST', self.generation_model_digest)):
            if digest is not None and not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError(f'{name} must be a lowercase 64-character SHA-256 digest')
        if self.model_provider == 'vllm' and (self.embedding_model_digest is None or self.generation_model_digest is None):
            raise ValueError('vLLM requires TRACEDESK_EMBED_MODEL_DIGEST and TRACEDESK_CHAT_MODEL_DIGEST')

    @classmethod
    def load(cls, root: Path = ROOT, environ: Mapping[str, str] | None = None) -> Settings:
        environment = os.environ if environ is None else environ
        path = root / '.env'
        file_values = dotenv_values(path, encoding='utf-8-sig', interpolate=False) if path.is_file() else {}
        unknown = {key for key in file_values if key.startswith('TRACEDESK_') and key not in KEYS}
        unknown |= {key for key in environment if key.startswith('TRACEDESK_') and key not in KEYS | TEST_KEYS}
        if unknown:
            raise ValueError('未知配置项：' + ', '.join(sorted(unknown)))

        def value(key: str, default: str) -> str:
            chosen = environment[key] if key in environment else file_values.get(key, default)
            if chosen is None or not chosen.strip():
                raise ValueError(f'{key} 不能为空。')
            return chosen.strip()

        def optional_value(key: str) -> str | None:
            chosen = environment[key] if key in environment else file_values.get(key)
            if chosen is None:
                return None
            if not chosen.strip():
                raise ValueError(f'{key} 不能为空。')
            return chosen.strip()

        def secret(key: str) -> str | None:
            file_key = key + '_FILE'
            source = environment if key in environment or file_key in environment else file_values
            direct, filename = source.get(key), source.get(file_key)
            if direct is not None and filename is not None:
                raise ValueError(f'Configure only one of {key} and {file_key}')
            if filename is not None:
                try:
                    raw = Path(filename).read_bytes()
                    if len(raw) > 4096:
                        raise ValueError
                    direct = raw.decode('utf-8-sig').strip()
                except (OSError, UnicodeError, ValueError):
                    raise ValueError(f'{file_key} must reference a readable bounded UTF-8 secret') from None
            if direct is not None and not direct.strip():
                raise ValueError(f'{key} cannot be empty')
            return direct.strip() if direct is not None else None

        data_dir = Path(value('TRACEDESK_DATA_DIR', 'data')).expanduser()
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        try:
            port = int(value('TRACEDESK_PORT', '8765'))
        except ValueError as exc:
            raise ValueError('TRACEDESK_PORT 必须是 1 至 65535 之间的整数。') from exc
        database_url = secret('DATABASE_URL')
        if database_url is not None and not database_url.strip():
            raise ValueError('DATABASE_URL 不能为空。')
        model_provider = value('TRACEDESK_MODEL_PROVIDER', 'ollama')
        embedding_default = ('Qwen/Qwen3-Embedding-0.6B' if model_provider == 'vllm'
                             else 'qwen3-embedding:0.6b-q8_0')
        generation_default = ('Qwen/Qwen3-4B-Instruct-2507' if model_provider == 'vllm'
                              else 'qwen3.5:4b-q4_K_M')
        return cls(data_dir=data_dir.resolve(), port=port, database_url=database_url,
                   public_origin=value('TRACEDESK_PUBLIC_ORIGIN', 'https://localhost'),
                   bootstrap_token=secret('TRACEDESK_BOOTSTRAP_TOKEN'),
                   deployment=value('TRACEDESK_DEPLOYMENT', 'development'),
                   model_provider=model_provider,
                   ollama_url=local_ollama_url(value('TRACEDESK_OLLAMA_URL', 'http://127.0.0.1:11434')),
                   vllm_chat_url=local_vllm_url(value('TRACEDESK_VLLM_CHAT_URL', 'http://127.0.0.1:8000')),
                   vllm_embed_url=local_vllm_url(value('TRACEDESK_VLLM_EMBED_URL', 'http://127.0.0.1:8001')),
                   embedding_model=value('TRACEDESK_EMBED_MODEL', embedding_default),
                   generation_model=value('TRACEDESK_CHAT_MODEL', generation_default),
                   embedding_model_digest=optional_value('TRACEDESK_EMBED_MODEL_DIGEST'),
                   generation_model_digest=optional_value('TRACEDESK_CHAT_MODEL_DIGEST'),
                   vllm_api_key=secret('TRACEDESK_VLLM_API_KEY'))
