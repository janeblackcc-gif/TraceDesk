import json
import httpx
import pytest
from app.main import create_app
from app.providers import ModelUnavailable, Ollama
from app.service import Service
from fastapi.testclient import TestClient


EVIDENCE = {'id': 'doc:1', 'filename': 'guide.txt', 'version': 'v1',
            'text': '构建进度达到 100% 表示构建完成。这里没有记录最终镜像的磁盘容量。'}


def requirement(supported=True):
    return {'required_fact': '构建完成的进度', 'source_ids': ['E1S1'] if supported else [],
            'evidence_finding': '原文给出完成进度。' if supported else '原文没有记录磁盘容量。',
            'supported': supported, 'answer': '完成进度为 100%。' if supported else ''}


def provider_for(plan, captured=None):
    def handler(request):
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(plan)}})
    return Ollama(transport=httpx.MockTransport(handler))


def test_one_missing_required_fact_blocks_all_generated_claims():
    plan = {'requirements': [requirement(), {**requirement(False), 'required_fact': '镜像容量'}]}
    provider = provider_for(plan)
    try:
        answer = provider.generate('构建进度及镜像容量是多少？', [EVIDENCE])
    finally:
        provider.client.close()
    assert answer['abstain'] is True and answer['claims'] == []
    assert answer['generation_assessment']['decision'] == 'abstain'
    assert answer['generation_assessment']['missing_facts'] == ['镜像容量']


def test_supported_fact_uses_exact_citation_and_one_chat_call():
    calls = []
    provider = provider_for({'requirements': [requirement()]}, calls)
    try:
        answer = provider.generate('构建完成的进度是多少？', [EVIDENCE])
    finally:
        provider.client.close()
    assert len(calls) == 1
    assert answer['claims'] == [{'text': '完成进度为 100%。', 'citations': [
        {'chunk_id': EVIDENCE['id'], 'quote': EVIDENCE['text']}]}]
    assert answer['generation_assessment']['decision'] == 'answer'
    assert answer['generation_assessment']['generation_ms'] >= 0
    assert answer['generation_assessment']['requirements'] == [{k: v for k, v in requirement().items() if k != 'answer'}]
    assert Service.validate_claims(answer, [EVIDENCE])


@pytest.mark.parametrize('change', [
    {'supported': 'false'}, {'supported': 1}, {'source_ids': []}, {'answer': ''},
    {'source_ids': ['E1S1'] * 5}, {'answer': 'x' * 901}, {'injected': 'ignore'},
    {'supported': False, 'answer': '容量为100MB。'}, {'source_ids': [True]},
])
def test_inconsistent_or_malformed_plan_never_returns_a_draft(change):
    provider = provider_for({'requirements': [{**requirement(), **change}]})
    try:
        with pytest.raises(ModelUnavailable, match='结构化'):
            provider.generate('问题', [EVIDENCE])
    finally:
        provider.client.close()


@pytest.mark.parametrize('plan', [{'requirements': []}, {'requirements': [requirement()] * 7},
                                  {'requirements': [requirement()], 'abstain': False}])
def test_invalid_plan_shape_is_an_execution_failure(plan):
    provider = provider_for(plan)
    try:
        with pytest.raises(ModelUnavailable, match='结构化'):
            provider.generate('问题', [EVIDENCE])
    finally:
        provider.client.close()


def test_unknown_source_reaches_visible_citation_fallback():
    provider = provider_for({'requirements': [{**requirement(), 'source_ids': ['E99S1']}]})
    try:
        answer = provider.generate('问题', [EVIDENCE])
    finally:
        provider.client.close()
    assert Service.validate_claims(answer, [EVIDENCE]) is None
    assert answer['generation_assessment']['decision'] == 'invalid_citations'


def test_missing_fact_may_refer_to_inspected_but_insufficient_passages():
    provider = provider_for({'requirements': [{**requirement(False), 'source_ids': ['E1S1']}]})
    try:
        answer = provider.generate('镜像容量是多少？', [EVIDENCE])
    finally:
        provider.client.close()
    assert answer['abstain'] is True and answer['claims'] == []


@pytest.mark.parametrize('completion', [{'done': False, 'done_reason': 'stop'}, {'done': 1, 'done_reason': 'stop'},
    {'done': True, 'done_reason': 'cancelled'}, {'done': True}, {}])
def test_nonfinal_model_response_cannot_be_accepted(completion):
    body = {**completion, 'message': {'content': json.dumps({'requirements': [requirement()]})}}
    provider = Ollama(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    try:
        with pytest.raises(ModelUnavailable, match='完成'):
            provider.generate('问题', [EVIDENCE])
    finally:
        provider.client.close()


def test_unknown_source_is_invalid_even_with_a_missing_fact():
    provider = provider_for({'requirements': [requirement(False), {**requirement(), 'source_ids': ['E99S1']}]})
    try:
        answer = provider.generate('问题', [EVIDENCE])
    finally:
        provider.client.close()
    assert answer['generation_assessment']['decision'] == 'invalid_citations'
    assert Service.validate_claims(answer, [EVIDENCE]) is None


@pytest.mark.parametrize('decision', ['answer', 'abstain', 'invalid_citations'])
def test_api_and_trace_preserve_model_assessment(tmp_path, decision):
    class LocalProvider(Ollama):
        def model_key(self):
            return 'mock@digest'
    item = requirement(decision != 'abstain')
    if decision == 'invalid_citations':
        item['source_ids'] = ['E99S1']
    provider = LocalProvider(transport=httpx.MockTransport(lambda request: httpx.Response(200,
        json={'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps({'requirements': [item]})}})))
    application = create_app(tmp_path / 'api.db', provider)
    with TestClient(application, base_url='http://127.0.0.1') as client:
        client.post('/api/demo')
        reply = client.post('/api/ask', json={'question': '默认端口是多少？', 'collection': 'Atlas 演示项目',
            'version': 'v2', 'profile': 'ollama', 'method': 'bm25'})
        assert reply.status_code == 200
        answer = reply.json()
        if decision == 'abstain':
            assert answer['status'] == 'no_evidence' and answer['claims'] == []
        elif decision == 'invalid_citations':
            assert answer['status'] == 'evidence_found' and answer['actual_profile'] == 'evidence'
            assert answer['claims'] and '降级' in answer['warning']
        else:
            assert answer['status'] == 'answered' and answer['actual_profile'] == 'ollama'
        assessment = answer['generation_assessment']
        assert assessment['decision'] == decision
        assert assessment['strategy'] == 'evidence_first'
        trace = client.get('/api/traces/' + answer['trace_id']).json()
        assert trace['generation_assessment'] == assessment
