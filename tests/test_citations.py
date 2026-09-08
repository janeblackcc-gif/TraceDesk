import json
import httpx
import pytest
from app.providers import Ollama
from app.service import Service


EVIDENCE = {'id': 'document:1', 'filename': 'guide.md', 'version': 'v1',
            'text': '程序不会自动读取 .env 文件；.env.example 仅供参考。'}


def reply(citation):
    return {'abstain': False, 'claims': [{'text': '程序不会自动读取 .env 文件。', 'citations': [citation]}]}


def generate(result, evidence=None):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'message': {'content': json.dumps(result, ensure_ascii=False)}})
    provider = Ollama(transport=httpx.MockTransport(handler))
    try:
        answer = provider.generate('是否自动读取 .env？', [EVIDENCE] if evidence is None else evidence)
        return answer, requests[0]
    finally:
        provider.client.close()


def test_citations_are_materialized_from_selected_source_without_model_copying():
    answer, request = generate(reply({'source_id': 'E1S1'}))
    assert answer['claims'][0]['citations'] == [{'chunk_id': EVIDENCE['id'], 'quote': EVIDENCE['text']}]
    assert Service.validate_claims(answer, [EVIDENCE]) is not None
    context = json.loads(request['messages'][1]['content'])['evidence']
    assert context[0]['passages'] == [{'source_id': 'E1S1', 'text': EVIDENCE['text']}]
    allowed = request['format']['properties']['claims']['items']['properties']['citations']['items']['properties']['source_id']['enum']
    assert allowed == ['E1S1']


@pytest.mark.parametrize('citation', [{'source_id': 'E9S9'}, {'source_id': 1},
    {'source_id': 'E1S1', 'quote': 'Changed numbers or spaces'}, {'chunk_id': 'document:1', 'quote': EVIDENCE['text']}])
def test_unknown_or_unexpected_citations_cannot_bypass_source_selection(citation):
    answer, _ = generate(reply(citation))
    assert Service.validate_claims(answer, [EVIDENCE]) is None


def test_long_source_is_split_into_exact_bounded_passages():
    evidence = {**EVIDENCE, 'text': '配置值 ABC 123 保留空格。' * 110}
    answer, request = generate(reply({'source_id': 'E1S1'}), [evidence])
    passages = json.loads(request['messages'][1]['content'])['evidence'][0]['passages']
    assert len(passages) > 1
    assert all(8 <= len(p['text']) <= 600 and p['text'] in evidence['text'] for p in passages)
    assert Service.validate_claims(answer, [evidence]) is not None


def test_abstention_remains_an_abstention():
    answer, _ = generate({'abstain': True, 'claims': []})
    assert answer == {'abstain': True, 'claims': []}


def test_source_selection_does_not_validate_claim_semantics():
    raw = reply({'source_id': 'E1S1'})
    raw['claims'][0]['text'] = 'The port is 9999.'
    answer, _ = generate(raw)
    assert Service.validate_claims(answer, [EVIDENCE]) is not None
    assert answer['claims'][0]['text'] == 'The port is 9999.'
