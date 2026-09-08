"""Bounded parsing. Line numbers refer to extracted text, not PDF geometry."""
from __future__ import annotations
import io
import re
from pathlib import Path
from pypdf import PdfReader

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
MAX_TEXT = 1_000_000
MAX_CHUNKS = 2500
SUSPICIOUS = re.compile(
    r'ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|'
    r'忽略.{0,12}(?:之前|系统|以上).{0,8}(?:指令|提示)|'
    r'<\s*script\b|(?:reveal|泄露).{0,20}(?:system prompt|系统提示)', re.I)

class InputError(ValueError):
    pass

def normalize(text: str) -> str:
    return text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')

def parse(name: str, data: bytes) -> tuple[str, list[str]]:
    if not data or len(data) > MAX_BYTES:
        raise InputError('文件须非空且不超过 10 MiB。')
    clean_name = Path(name.replace('\\', '/')).name[:180]
    suffix = Path(clean_name).suffix.lower()
    if suffix in {'.md', '.txt'}:
        try:
            pages = [normalize(data.decode('utf-8-sig'))]
        except UnicodeDecodeError as exc:
            raise InputError('文本必须采用 UTF-8 编码，请另存为 UTF-8 后重试。') from exc
    elif suffix == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise InputError('扩展名为 PDF，但文件头无效。')
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise InputError('首版不支持加密 PDF，请导出解密副本。')
            if len(reader.pages) > MAX_PAGES:
                raise InputError('PDF 最多 100 页，请拆分文件。')
            pages = [normalize(page.extract_text() or '') for page in reader.pages]
        except InputError:
            raise
        except Exception as exc:
            raise InputError('PDF 解析失败；请尝试导出为文本型 PDF 或 Markdown。') from exc
        if not pages or any(len(p.strip()) < 8 for p in pages):
            raise InputError('PDF 含空白或无可提取文字的页面；首版不支持 OCR。请移除空白页或导出文本。')
    else:
        raise InputError('仅支持 .md、.txt、文本型 .pdf；不接收 ZIP 或可执行文件。')
    if not any(p.strip() for p in pages):
        raise InputError('未检测到可索引文字。')
    if sum(map(len, pages)) > MAX_TEXT:
        raise InputError('解析文本超过 100 万字符，请拆分文档。')
    if any(len(line) > 4096 for p in pages for line in p.splitlines()):
        raise InputError('存在超过 4096 字符的单行，请先格式化文档。')
    return clean_name, pages

def chunks(pages: list[str]) -> list[dict]:
    """Heading-aware line chunks, soft 850 chars; at most 2 overlap lines.

    No semantic model and no table/OCR reconstruction is claimed.
    """
    output = []
    for page_number, text in enumerate(pages, 1):
        lines = text.split('\n')
        heading = ''
        start = 0
        while start < len(lines):
            while start < len(lines) and not lines[start].strip():
                start += 1
            if start >= len(lines):
                break
            if re.match(r'^#{1,6}\s+', lines[start]):
                heading = re.sub(r'^#+\s*', '', lines[start])[:160]
            end, length = start, 0
            while end < len(lines):
                line = lines[end]
                if end > start and re.match(r'^#{1,6}\s+', line):
                    break
                if end > start and length + len(line) > 850:
                    break
                length += len(line) + 1
                end += 1
            content = '\n'.join(lines[start:end]).strip()
            if content:
                output.append({'page': page_number, 'start_line': start + 1,
                               'end_line': end, 'heading': heading, 'text': content})
            if len(output) > MAX_CHUNKS:
                raise InputError('分块超过 2500 块，请拆分文档。')
            # Never overlap into the next heading; ensure strict forward progress.
            at_heading = end < len(lines) and re.match(r'^#{1,6}\s+', lines[end])
            start = end if at_heading or end >= len(lines) else max(start + 1, end - 2)
    return output
