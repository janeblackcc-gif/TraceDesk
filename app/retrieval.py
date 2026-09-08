"""Inspectable retrieval baselines; no ML is claimed by the lexical channel."""
from __future__ import annotations
import math
import re
from collections import Counter

STOP = ('请问', '如何', '怎么', '什么', '为什么', '哪些', '多少', '是否', '怎样', '可以',
        '应该', '一下', '告诉我', '需要', '我们', '这个', '那个', '当前', '进行', '使用', '的', '了', '吗', '呢')

def tokenize(text: str) -> list[str]:
    text = text.lower()
    # Keep identifiers, dotted versions, flags and error codes; Chinese uses character bigrams.
    for word in STOP:
        text = text.replace(word, ' ')
    out = re.findall(r'[a-z0-9_]+(?:[.\-][a-z0-9_]+)*', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        out.extend([run[i:i + 2] for i in range(len(run) - 1)] if len(run) > 1 else [run])
    return out

def document_text(c: dict) -> str:
    return f"{c['filename']}\n{c['heading']}\n{c['text']}"

def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or not all(math.isfinite(v) for v in a + b):
        raise ValueError('向量维度或数值无效；请重建索引。')
    denominator = math.sqrt(sum(v*v for v in a) * sum(v*v for v in b))
    return sum(x*y for x, y in zip(a, b)) / denominator if denominator else 0.0

def search(question: str, candidates: list[dict], method: str = 'hybrid',
           vectors: dict[str, list[float]] | None = None, query_vector: list[float] | None = None,
           top_k: int = 5) -> list[dict]:
    if not candidates:
        return []
    terms = set(tokenize(question))
    if not terms:
        return []
    counts = [Counter(tokenize(document_text(c))) for c in candidates]
    n = len(counts)
    df = Counter(t for count in counts for t in count)
    average = sum(sum(c.values()) for c in counts) / n or 1
    idf = {t: math.log(1 + (n - df[t] + .5) / (df[t] + .5)) for t in terms}
    sparse_idf = {t: math.log((n + 1) / (freq + 1)) + 1 for t, freq in df.items()}
    q_sparse = {t: sparse_idf.get(t, math.log(n + 1) + 1) for t in terms}
    q_norm = math.sqrt(sum(x*x for x in q_sparse.values())) or 1
    scores = []
    for c, tf in zip(candidates, counts):
        length = sum(tf.values())
        bm25 = sum(idf[t] * tf[t] * 2.5 / (tf[t] + 1.5 * (.25 + .75 * length / average))
                   for t in terms if tf[t])
        if vectors is not None and query_vector is not None:
            secondary = cosine(query_vector, vectors[c['id']])
            secondary_kind = 'dense'
        else:
            weights = {t: (1 + math.log(freq)) * sparse_idf[t] for t, freq in tf.items()}
            denom = math.sqrt(sum(v*v for v in weights.values())) * q_norm
            secondary = sum(weights.get(t, 0) * w for t, w in q_sparse.items()) / denom if denom else 0
            secondary_kind = 'lexical_tfidf'
        scores.append({**c, 'bm25': bm25, 'secondary': secondary, 'secondary_kind': secondary_kind,
                       'coverage': len(terms & tf.keys()) / len(terms)})
    def ranking(key: str, threshold: float):
        return sorted([s for s in scores if s[key] > threshold], key=lambda s: (-s[key], s['id']))[:20]
    bm = ranking('bm25', 0)
    other = ranking('secondary', .20 if vectors is not None else 0)
    if method == 'bm25':
        combined = [(s, s['bm25']) for s in bm]
    elif method == 'dense':
        if vectors is None:
            raise ValueError('dense 检索只在本地模型索引完成后可用。')
        combined = [(s, s['secondary']) for s in other]
    else:
        rrf = Counter()
        union = {}
        for channel in (bm, other):
            for rank, s in enumerate(channel, 1):
                rrf[s['id']] += 1 / (60 + rank)
                union[s['id']] = s
        combined = [(union[cid], value) for cid, value in rrf.items()]
    # Heuristic relevance gate, NOT calibrated probability. Keep dense semantic-only matches at >=.50.
    combined = [(s, value) for s, value in combined if s['coverage'] >= .12 or
                (vectors is not None and s['secondary'] >= .50)]
    combined.sort(key=lambda pair: (-pair[1], pair[0]['id']))
    return [{**s, 'score': round(value, 6), 'rank': i + 1} for i, (s, value) in enumerate(combined[:top_k])]
