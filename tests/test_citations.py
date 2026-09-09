import json
import httpx
import pytest
from app.providers import Ollama, ModelUnavailable
from app.service import Service
from app.citations import MAX_QUOTE_CHARS, quote_passages


EVIDENCE = {'id': 'document:1', 'filename': 'guide.md', 'version': 'v1',
            'text': '程序不会自动读取 .env 文件；.env.example 仅供参考。'}


def reply(source_id):
    return {'requirements': [{'required_fact': '是否自动读取 .env', 'source_ids': [source_id],
        'evidence_finding': '原文明示不会自动读取。', 'supported': True, 'answer': '程序不会自动读取 .env 文件。'}]}


def generate(result, evidence=None):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(result, ensure_ascii=False)}})
    provider = Ollama(transport=httpx.MockTransport(handler))
    try:
        answer = provider.generate('是否自动读取 .env？', [EVIDENCE] if evidence is None else evidence)
        return answer, requests[0]
    finally:
        provider.client.close()


def test_citations_are_materialized_from_selected_source_without_model_copying():
    answer, request = generate(reply('E1S1'))
    assert answer['claims'][0]['citations'] == [{'chunk_id': EVIDENCE['id'], 'quote': EVIDENCE['text']}]
    assert Service.validate_claims(answer, [EVIDENCE]) is not None
    context = json.loads(request['messages'][1]['content'])['evidence']
    assert context[0]['passages'] == [{'source_id': 'E1S1', 'text': EVIDENCE['text']}]
    allowed = request['format']['$defs']['EvidenceRequirement']['properties']['source_ids']['items']['enum']
    assert allowed == ['E1S1']


def test_unknown_citations_cannot_bypass_source_selection():
    answer, _ = generate(reply('E9S9'))
    assert Service.validate_claims(answer, [EVIDENCE]) is None


@pytest.mark.parametrize('source', [1, {'source_id': 'E1S1', 'quote': 'Changed numbers or spaces'},
    {'chunk_id': 'document:1', 'quote': EVIDENCE['text']}])
def test_model_cannot_supply_quote_text_or_other_source_types(source):
    with pytest.raises(ModelUnavailable):
        generate(reply(source))


def test_long_source_is_split_into_exact_bounded_passages():
    evidence = {**EVIDENCE, 'text': '配置值 ABC 123 保留空格。' * 110}
    answer, request = generate(reply('E1S1'), [evidence])
    passages = json.loads(request['messages'][1]['content'])['evidence'][0]['passages']
    assert len(passages) > 1
    assert all(8 <= len(p['text']) <= MAX_QUOTE_CHARS and p['text'] in evidence['text'] for p in passages)
    assert Service.validate_claims(answer, [evidence]) is not None


def test_normal_ingestion_chunk_is_not_split_from_its_qualifier():
    text = 'An earlier section discusses computation.\n' * 17 + 'The noise is complex Gaussian.'
    assert 600 < len(text) < 850
    assert quote_passages(text) == [text]


def test_abstention_remains_an_abstention():
    answer, _ = generate({'requirements': [{'required_fact': '镜像容量', 'source_ids': [],
        'evidence_finding': '没有容量记录。', 'supported': False, 'answer': ''}]})
    assert answer['abstain'] is True and answer['claims'] == []
    assert answer['generation_assessment']['decision'] == 'abstain'


def test_source_selection_does_not_validate_claim_semantics():
    raw = reply('E1S1')
    raw['requirements'][0]['answer'] = 'The port is 9999.'
    answer, _ = generate(raw)
    assert Service.validate_claims(answer, [EVIDENCE]) is not None
    assert answer['claims'][0]['text'] == 'The port is 9999.'
