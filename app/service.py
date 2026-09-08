from __future__ import annotations
import json
import re
import threading
import time
import uuid
from pathlib import Path
from .ingest import InputError
from .store import Store
from .providers import Ollama, ModelUnavailable
from .retrieval import search, document_text

ROOT = Path(__file__).resolve().parents[1]

class IndexRequired(InputError):
    pass

class Service:
    def __init__(self, path: str | Path, provider=None):
        self.store = Store(path)
        self.provider = provider or Ollama()
        self.lock = threading.RLock()

    def load_demo(self) -> dict:
        manifest = json.loads((ROOT / 'demo/manifest.json').read_text('utf-8'))
        results = []
        for doc in manifest['documents']:
            results.append(self.store.import_document(doc['filename'], (ROOT / 'demo' / doc['file']).read_bytes(),
                                                      doc['collection'], doc['version']))
        self.store.activate('Atlas 演示项目', 'v2')
        return {'documents': len(results), 'note': '原创虚构技术文档，仅用于演示和回归测试。', 'results': results}

    def index(self, collection: str, version: str | None) -> dict:
        version = self.store.resolve_version(collection, version)
        candidates = self.store.candidates(collection, version)
        if not candidates:
            raise InputError('该范围没有可索引的有效文档。')
        key = self.provider.model_key()
        existing = self.store.get_vectors([c['id'] for c in candidates], key)
        missing = [c for c in candidates if c['id'] not in existing]
        pending = {}
        for start in range(0, len(missing), 8):
            batch = missing[start:start + 8]
            values = self.provider.embed([document_text(c) for c in batch])
            pending.update({c['id']: v for c, v in zip(batch, values)})
        dims = {len(v) for v in [*existing.values(), *pending.values()]}
        if len(dims) > 1:
            raise ModelUnavailable('索引向量维度不一致，未保存新索引。请核对模型版本。')
        self.store.save_vectors(pending, key)
        return {'version': version, 'indexed': len(pending), 'total': len(candidates), 'model_key': key,
                'dimension': next(iter(dims), 0)}

    @staticmethod
    def validate_claims(result: dict, evidence: list[dict]) -> list[dict] | None:
        if not isinstance(result, dict) or type(result.get('abstain')) is not bool:
            return None
        if result['abstain']:
            return [] if result.get('claims') == [] else None
        claims = result.get('claims')
        if not isinstance(claims, list) or not 1 <= len(claims) <= 6:
            return None
        allowed = {c['id']: c for c in evidence}
        validated = []
        for claim in claims:
            if not isinstance(claim, dict):
                return None
            text, citations = claim.get('text'), claim.get('citations')
            if not isinstance(text, str) or not text.strip() or len(text) > 900 or not isinstance(citations, list) or not 1 <= len(citations) <= 4:
                return None
            checked = []
            for cite in citations:
                if not isinstance(cite, dict):
                    return None
                chunk_id, quote = cite.get('chunk_id'), cite.get('quote')
                if not isinstance(chunk_id, str) or chunk_id not in allowed or not isinstance(quote, str) or not 8 <= len(quote) <= 600:
                    return None
                if quote not in allowed[chunk_id]['text']:
                    return None
                checked.append({'chunk_id': chunk_id, 'quote': quote})
            validated.append({'text': text.strip(), 'citations': checked})
        return validated

    def ask(self, question: str, collection: str, version: str | None = None, profile: str = 'evidence',
            method: str = 'hybrid', conversation_id: str | None = None) -> dict:
        question = question.strip()
        if not question or len(question) > 1000:
            raise InputError('问题长度须为 1—1000 字符。')
        if profile not in {'evidence', 'ollama'} or method not in {'bm25', 'hybrid', 'dense'}:
            raise InputError('未知模型或检索模式。')
        if profile == 'evidence' and method == 'dense':
            raise InputError('证据模式不包含稠密向量；请先接入本地模型。')
        started = time.perf_counter()
        version = self.store.resolve_version(collection, version)
        cid = conversation_id or uuid.uuid4().hex
        response = {'question': question, 'effective_question': question, 'collection': collection, 'version': version,
                    'conversation_id': cid, 'requested_profile': profile, 'actual_profile': profile,
                    'claims': [], 'sources': [], 'warning': '', 'status': 'answered', 'method': method,
                    'citation_check': '仅核验引用位置与逐字摘录；不等于语义正确性验证。'}
        mentioned_versions = set(re.findall(r'(?<![A-Za-z0-9_])v\d+(?:\.\d+)*(?![A-Za-z0-9_])', question, re.I))
        if mentioned_versions and {v.lower() for v in mentioned_versions} != {version.lower()}:
            response.update(status='needs_scope', warning='问题提到了其他版本；请先切换版本。首版不自动跨版本合并。')
            return self._finish(response, started)
        previous = self.store.previous(cid, collection, version)
        followup = bool(re.match(r'^(那|它|这个|这一步|上述|刚才)', question))
        if followup and not previous:
            response.update(status='clarify', warning='缺少同一知识库和版本的上文，请写出具体组件或问题。')
            return self._finish(response, started)
        effective = f'{previous}\n追问：{question}' if followup and previous else question
        response['effective_question'] = effective
        candidates = self.store.candidates(collection, version)
        key, vectors, qv = None, None, None
        if profile == 'ollama':
            key = self.provider.model_key()
            if method != 'bm25':
                vectors = self.store.get_vectors([c['id'] for c in candidates], key)
                if len(vectors) != len(candidates) or not candidates:
                    raise IndexRequired('当前版本的向量索引缺失或已过期，请在知识库页建立本地向量索引。')
                qv = self.provider.embed(['Instruct: Retrieve technical documentation passages that answer the question.\nQuery: ' + effective])[0]
        retrieved = search(effective, candidates, method, vectors, qv)
        response['retrieval_ms'] = round((time.perf_counter() - started) * 1000, 2)
        response['model_key'] = key
        response['secondary_channel'] = 'dense' if vectors is not None else 'lexical_tfidf'
        response['sources'] = retrieved
        if not retrieved:
            response.update(status='no_evidence', warning='所选版本未检索到足够相关的证据。请补充文档、改写问题或检查版本。')
            return self._finish(response, started)
        evidence = retrieved[:4]
        if profile == 'ollama':
            result = self.provider.generate(effective, evidence)
            claims = self.validate_claims(result, evidence)
            if claims is None:
                response.update(actual_profile='evidence', warning='模型引用校验失败，已明确降级为原文摘录；下方不是模型生成结论。')
            elif not claims:
                response.update(status='no_evidence', warning='模型判断现有证据不足，未生成回答。')
                return self._finish(response, started)
            else:
                response['claims'] = claims
                response['generation_model'] = self.provider.generation
        if response['actual_profile'] == 'evidence':
            response['status'] = 'evidence_found'
            response['claims'] = [{'text': c['text'][:450], 'citations': [{'chunk_id': c['id'], 'quote': c['text'][:450]}]} for c in evidence]
        self.store.remember(cid, collection, version, question)
        return self._finish(response, started)

    def _finish(self, response: dict, started: float) -> dict:
        response['latency_ms'] = round((time.perf_counter() - started) * 1000, 2)
        payload = {k: v for k, v in response.items() if k not in {'claims', 'sources'}}
        payload['retrieved'] = [{k: s[k] for k in ('id', 'filename', 'version', 'rank', 'score')} for s in response['sources']]
        response['trace_id'] = self.store.trace(payload)
        return response
