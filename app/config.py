"""Project-local configuration with explicit precedence and validation."""
from __future__ import annotations
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
KEYS = {'TRACEDESK_DATA_DIR', 'TRACEDESK_PORT', 'TRACEDESK_OLLAMA_URL',
        'TRACEDESK_EMBED_MODEL', 'TRACEDESK_CHAT_MODEL'}


def local_ollama_url(value: str) -> str:
    value = value.strip().rstrip('/')
    parsed = urlparse(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or
            parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('Ollama 地址仅允许本机 HTTP 地址，不支持远程、凭据、路径或查询参数。')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Ollama 端口必须在 1 至 65535 之间。')
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int = 8765
    ollama_url: str = 'http://127.0.0.1:11434'
    embedding_model: str = 'qwen3-embedding:0.6b-q8_0'
    generation_model: str = 'qwen3.5:4b-q4_K_M'

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('TRACEDESK_PORT 必须是 1 至 65535 之间的整数。')
        local_ollama_url(self.ollama_url)
        for name in (self.embedding_model, self.generation_model):
            if not name or any(character.isspace() for character in name):
                raise ValueError('模型标签不能为空或包含空白字符。')
            if 'cloud' in name.lower():
                raise ValueError('请使用本地模型标签，不支持 cloud 模型。')

    @classmethod
    def load(cls, root: Path = ROOT, environ: Mapping[str, str] | None = None) -> Settings:
        environment = os.environ if environ is None else environ
        path = root / '.env'
        file_values = dotenv_values(path, encoding='utf-8-sig', interpolate=False) if path.is_file() else {}
        unknown = {key for key in file_values if key.startswith('TRACEDESK_') and key not in KEYS}
        if unknown:
            raise ValueError('未知配置项：' + ', '.join(sorted(unknown)))

        def value(key: str, default: str) -> str:
            chosen = environment[key] if key in environment else file_values.get(key, default)
            if chosen is None or not chosen.strip():
                raise ValueError(f'{key} 不能为空。')
            return chosen.strip()

        data_dir = Path(value('TRACEDESK_DATA_DIR', 'data')).expanduser()
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        try:
            port = int(value('TRACEDESK_PORT', '8765'))
        except ValueError as exc:
            raise ValueError('TRACEDESK_PORT 必须是 1 至 65535 之间的整数。') from exc
        return cls(data_dir=data_dir.resolve(), port=port,
                   ollama_url=local_ollama_url(value('TRACEDESK_OLLAMA_URL', 'http://127.0.0.1:11434')),
                   embedding_model=value('TRACEDESK_EMBED_MODEL', 'qwen3-embedding:0.6b-q8_0'),
                   generation_model=value('TRACEDESK_CHAT_MODEL', 'qwen3.5:4b-q4_K_M'))
