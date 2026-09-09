"""Resolve model-selected evidence IDs to exact text from the current request."""
from __future__ import annotations
from dataclasses import dataclass

MAX_QUOTE_CHARS = 900

@dataclass(frozen=True)
class CitationSource:
    chunk_id: str
    quote: str


def quote_passages(text: str) -> list[str]:
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        # Normal ingestion chunks fit in one quote, preserving formulas and nearby qualifiers.
        end = min(start + 850, len(text))
        if end < len(text):
            boundary = text.rfind('\n', start + 8, end)
            if boundary != -1:
                end = boundary + 1
            if len(text) - end < 8:
                end = len(text)
        if len(text[start:end].strip()) < 8:
            if end < len(text):
                end = min(start + MAX_QUOTE_CHARS, len(text))
            elif ranges and end - ranges[-1][0] <= MAX_QUOTE_CHARS:
                ranges[-1] = (ranges[-1][0], end)
                break
        if len(text[start:end].strip()) >= 8:
            ranges.append((start, end))
        start = end
    return [text[start:end].strip() for start, end in ranges]


def prepare_evidence(evidence: list[dict]) -> tuple[list[dict], dict[str, CitationSource]]:
    context = []
    sources = {}
    for index, chunk in enumerate(evidence, 1):
        passages = []
        for part, quote in enumerate(quote_passages(chunk['text']), 1):
            source_id = f'E{index}S{part}'
            sources[source_id] = CitationSource(chunk['id'], quote)
            passages.append({'source_id': source_id, 'text': quote})
        context.append({'filename': chunk['filename'], 'version': chunk['version'],
                        **{key: chunk[key] for key in ('page', 'start_line', 'end_line') if key in chunk},
                        'passages': passages})
    return context, sources


def resolve_citations(result: dict, sources: dict[str, CitationSource]) -> dict | None:
    if type(result.get('abstain')) is not bool or not isinstance(result.get('claims'), list):
        return None
    if result['abstain']:
        return result
    claims = []
    for claim in result['claims']:
        if not isinstance(claim, dict) or not isinstance(claim.get('citations'), list):
            return None
        citations = []
        for citation in claim['citations']:
            if not isinstance(citation, dict) or set(citation) != {'source_id'}:
                return None
            source_id = citation['source_id']
            if not isinstance(source_id, str) or source_id not in sources:
                return None
            source = sources[source_id]
            citations.append({'chunk_id': source.chunk_id, 'quote': source.quote})
        claims.append({'text': claim.get('text'), 'citations': citations})
    return {'abstain': False, 'claims': claims}
