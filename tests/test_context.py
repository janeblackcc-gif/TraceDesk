import pytest
from app.retrieval import retrieval_queries, search_context, translation_language


def chunk(identifier, line, text, *, doc='one', version='v1', page=1):
    return {'id': identifier, 'doc_id': doc, 'filename': doc + '.txt', 'collection': 'test',
            'version': version, 'page': page, 'start_line': line, 'end_line': line,
            'heading': '', 'text': text}


def test_compound_queries_preserve_original_and_bound_subqueries():
    question = '一般运动模型是什么？实验中的噪声如何设定？请解释两者的区别。'
    parts = retrieval_queries(question)
    assert parts[0] == question and '实验中的噪声如何设定' in parts
    assert len(parts) <= 4 and len(parts) == len(set(parts))
    assert retrieval_queries('默认端口是多少？') == ['默认端口是多少？']


def test_semantic_matches_survive_without_chinese_lexical_overlap():
    # A relevant cross-language result need not have cosine >= .50.
    candidate = chunk('one:0', 1, 'The measurement noise is complex Gaussian.')
    result = search_context(['量测噪声服从什么分布'], [candidate], 'dense',
                            {candidate['id']: [.45, .893]}, [[1., 0.]])
    assert [item['id'] for item in result] == [candidate['id']]


def test_query_round_robin_retains_other_subquestion_anchor():
    candidates = [chunk(f'a:{i}', i, 'model description ' + str(i), doc=f'a{i}') for i in range(5)]
    candidates.append(chunk('noise:0', 1, 'experimental noise setting', doc='noise'))
    vectors = {c['id']: ([1., 0.] if c['id'] != 'noise:0' else [0., 1.]) for c in candidates}
    result = search_context(['运动模型是什么', '实验噪声如何设定'], candidates, 'dense', vectors,
                            [[1., 0.], [0., 1.]])
    assert result[0]['id'].startswith('a:') and result[1]['id'] == 'noise:0'


def test_adjacent_context_crosses_page_but_not_document_or_version():
    candidates = [chunk('one:9', 20, 'The update uses the following formula: alpha.', page=1),
                  chunk('one:10', 1, 's(t)=a(t)*max(b(t)*s(t-1))', page=2),
                  chunk('foreign:0', 1, 'wrong formula', doc='other', version='v2', page=2)]
    result = search_context(['alpha update'], candidates, 'bm25')
    ids = [c['id'] for c in result]
    assert 'one:9' in ids and 'one:10' in ids and 'foreign:0' not in ids
    neighbor = next(c for c in result if c['id'] == 'one:10')
    assert neighbor['context_origin'] == 'adjacent' and neighbor['context_anchor'] == 'one:9'


def test_no_anchor_does_not_turn_arbitrary_neighbors_into_evidence():
    candidates = [chunk('one:0', 1, 'The experiment uses a linear model.')]
    assert search_context(['海豚会唱什么歌曲'], candidates, 'bm25') == []


def test_context_is_unique_bounded_and_citation_text_unchanged():
    candidates = [chunk(f'one:{i}', i + 1, 'alpha ' + 'x' * 800) for i in range(40)]
    result = search_context(['alpha'], candidates, 'bm25')
    assert len(result) <= 18 and sum(len(c['text']) for c in result) <= 14000
    assert len({c['id'] for c in result}) == len(result)
    original = {c['id']: c for c in candidates}
    assert all(c['text'] == original[c['id']]['text'] for c in result)


def test_query_vectors_must_match_each_query():
    with pytest.raises(ValueError):
        search_context(['one', 'two'], [chunk('a', 1, 'one two')], 'dense', {'a': [1.]}, [[1.]])


def test_translation_is_only_requested_for_clear_language_mismatch():
    english = [chunk('one:0', 1, 'The measurement noise follows a complex Gaussian distribution.')]
    assert translation_language('实验中对于量测噪声做了什么假设？', english) == 'English'
    assert translation_language('What is the measurement noise distribution?', english) is None
    assert translation_language('E041', english) is None
