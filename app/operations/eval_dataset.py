"""Validation boundary for private, authorized, double-reviewed evaluation data."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ingest import InputError, chunks, parse

SHA256 = r'^[0-9a-f]{64}$'
MAX_METADATA_BYTES = 16 * 1024 * 1024
PLACEHOLDER_PATTERNS = (
    re.compile(
        r'^(?:generated\b.{0,200}\b(?:question|reference\s+answer|required\s+fact|forbidden\s+claim)'
        r'\b.{0,80}\bfor\b|(?:reference\s+answer|required\s+fact|forbidden\s+claim)\b.{0,80}\bfor\b)',
        re.IGNORECASE,
    ),
    re.compile(r'^(?:placeholder|todo|tbd)(?:\b|\s*[:：])', re.IGNORECASE),
    re.compile(r'^(?:占位|待填写|示例(?:问题|答案|事实)?)(?:\s*[:：]|\s|$)'),
)


def _is_placeholder(value: str) -> bool:
    normalized = ' '.join(value.split())
    return any(pattern.search(normalized) is not None for pattern in PLACEHOLDER_PATTERNS)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if not value or '\\' in value or path.is_absolute() or '..' in path.parts or value.endswith('/'):
        raise ValueError('Expected a normalized relative file path')
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CorpusDocument(StrictModel):
    path: str
    sha256: str = Field(pattern=SHA256)
    size_bytes: int = Field(ge=0)
    owner: str = Field(min_length=1, max_length=200)
    authorization_reference: str = Field(min_length=1, max_length=300)
    authorized_uses: list[Literal['evaluation', 'pilot', 'production']] = Field(min_length=1)
    authorization_expires_at: datetime | None = None

    @field_validator('path')
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        return relative_file(value)

    @model_validator(mode='after')
    def evaluation_is_authorized(self) -> CorpusDocument:
        if 'evaluation' not in self.authorized_uses:
            raise ValueError('Every corpus document must be authorized for evaluation')
        if len(set(self.authorized_uses)) != len(self.authorized_uses):
            raise ValueError('Authorized uses must be unique')
        if self.authorization_expires_at is not None and self.authorization_expires_at.utcoffset() is None:
            raise ValueError('Authorization expiry must include a timezone')
        return self


class CorpusManifest(StrictModel):
    format_version: Literal[1]
    corpus_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    created_at: datetime
    retention_policy_reference: str = Field(min_length=1, max_length=300)
    documents: list[CorpusDocument] = Field(min_length=1)

    @field_validator('created_at')
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError('created_at must include a timezone')
        return value


class EvalCase(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    split: Literal['dev', 'holdout']
    task_type: str = Field(min_length=1, max_length=100)
    answerability: Literal['answerable', 'unanswerable']
    severity: Literal['low', 'medium', 'high', 'blocker']
    source_group: str = Field(min_length=1, max_length=200)
    template_group: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)

    @field_validator('question')
    @classmethod
    def question_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('Question cannot be blank')
        return value.strip()


class EvidencePassage(StrictModel):
    document_sha256: str = Field(pattern=SHA256)
    quote: str = Field(min_length=8, max_length=4000)
    quote_sha256: str = Field(pattern=SHA256)

    @model_validator(mode='after')
    def quote_hash_matches(self) -> EvidencePassage:
        actual = hashlib.sha256(self.quote.strip().encode('utf-8')).hexdigest()
        if actual != self.quote_sha256:
            raise ValueError('Evidence quote hash mismatch')
        return self


class EvalLabel(StrictModel):
    case_id: str = Field(min_length=1, max_length=200)
    required_facts: list[str] = Field(min_length=1)
    evidence_groups: list[list[EvidencePassage]] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    reviewer_ids: list[str] = Field(min_length=1)
    dispute_status: Literal['none', 'unresolved', 'resolved'] = 'none'
    adjudicator_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode='after')
    def labels_are_coherent(self) -> EvalLabel:
        if any(not item.strip() for item in [*self.required_facts, *self.forbidden_claims, *self.reviewer_ids]):
            raise ValueError('Label text and reviewer IDs cannot be blank')
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids):
            raise ValueError('Reviewer IDs must be distinct')
        if any(not group for group in self.evidence_groups):
            raise ValueError('Evidence groups cannot be empty')
        if self.dispute_status == 'resolved' and not self.adjudicator_id:
            raise ValueError('Resolved disputes require an adjudicator')
        if self.dispute_status != 'resolved' and self.adjudicator_id is not None:
            raise ValueError('Adjudicator is only valid for a resolved dispute')
        return self


class DatasetManifest(StrictModel):
    format_version: Literal[2]
    dataset_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    created_at: datetime
    corpus_manifest_path: str
    corpus_manifest_sha256: str = Field(pattern=SHA256)
    cases_path: str
    cases_sha256: str = Field(pattern=SHA256)
    labels_path: str
    labels_sha256: str = Field(pattern=SHA256)
    authoring_status: Literal['draft', 'final'] = 'draft'
    semantic_review_status: Literal['pending', 'final'] = 'pending'
    sealed_holdout: bool
    split_policy: Literal['source_and_template_group']

    @field_validator('corpus_manifest_path', 'cases_path', 'labels_path')
    @classmethod
    def paths_are_relative(cls, value: str) -> str:
        return relative_file(value)

    @model_validator(mode='after')
    def files_are_distinct(self) -> DatasetManifest:
        if len({self.corpus_manifest_path, self.cases_path, self.labels_path}) != 3:
            raise ValueError('Manifest, cases and labels must be separate files')
        if self.created_at.utcoffset() is None:
            raise ValueError('created_at must include a timezone')
        return self


def _safe_file(root: Path, relative: str, *, metadata: bool = False) -> Path:
    path = root / relative
    current = root
    uses_symlink = False
    for part in PurePosixPath(relative).parts:
        current /= part
        uses_symlink = uses_symlink or current.is_symlink()
    if uses_symlink or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Dataset file escapes its root or uses a symlink')
    if not path.is_file():
        raise ValueError('Dataset file is missing')
    if metadata and path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError('Dataset metadata file exceeds 16 MiB')
    return path


def _jsonl(path: Path, model: type[StrictModel]) -> list[StrictModel]:
    rows = []
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), start=1):
        if line.strip():
            try:
                rows.append(model.model_validate(json.loads(line)))
            except Exception as exc:
                raise ValueError(f'Invalid JSONL row {number}') from exc
    if not rows:
        raise ValueError('JSONL input cannot be empty')
    return rows


def schema_bundle() -> dict[str, object]:
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'TraceDesk private evaluation dataset v2',
        'files': {
            'dataset_manifest.json': DatasetManifest.model_json_schema(),
            'corpus_manifest.json': CorpusManifest.model_json_schema(),
            'cases.jsonl_row': EvalCase.model_json_schema(),
            'labels.private.jsonl_row': EvalLabel.model_json_schema(),
        },
    }


def validate_dataset(directory: Path, *, formal: bool = False) -> dict[str, object]:
    root = directory.resolve()
    if directory.is_symlink() or not root.is_dir():
        raise ValueError('Dataset root must be a real directory')
    manifest_path = _safe_file(root, 'dataset_manifest.json', metadata=True)
    manifest = DatasetManifest.model_validate_json(manifest_path.read_text(encoding='utf-8-sig'))
    corpus_path = _safe_file(root, manifest.corpus_manifest_path, metadata=True)
    cases_path = _safe_file(root, manifest.cases_path, metadata=True)
    labels_path = _safe_file(root, manifest.labels_path, metadata=True)
    observed_hashes = {
        'corpus': sha256_file(corpus_path),
        'cases': sha256_file(cases_path),
        'labels': sha256_file(labels_path),
    }
    expected_hashes = {
        'corpus': manifest.corpus_manifest_sha256,
        'cases': manifest.cases_sha256,
        'labels': manifest.labels_sha256,
    }
    if observed_hashes != expected_hashes:
        raise ValueError('Dataset component hash mismatch')
    if formal and (manifest.authoring_status != 'final' or manifest.semantic_review_status != 'final'):
        raise ValueError('Formal release data requires final authoring and semantic review')
    corpus = CorpusManifest.model_validate_json(corpus_path.read_text(encoding='utf-8-sig'))
    cases = [item for item in _jsonl(cases_path, EvalCase) if isinstance(item, EvalCase)]
    labels = [item for item in _jsonl(labels_path, EvalLabel) if isinstance(item, EvalLabel)]
    if len({item.id for item in cases}) != len(cases):
        raise ValueError('Case IDs must be unique')
    if len({item.case_id for item in labels}) != len(labels) or {item.id for item in cases} != {item.case_id for item in labels}:
        raise ValueError('Cases and labels must have a one-to-one ID mapping')
    labels_by_case = {item.case_id: item for item in labels}
    documents_by_hash = {item.sha256: item for item in corpus.documents}
    if len(documents_by_hash) != len(corpus.documents) or len({item.path for item in corpus.documents}) != len(corpus.documents):
        raise ValueError('Corpus document paths and hashes must be unique')
    now = datetime.now(timezone.utc)
    document_paths: dict[str, Path] = {}
    chunks_by_document: dict[str, tuple[str, ...]] = {}
    for document in corpus.documents:
        path = _safe_file(root, document.path)
        if path.stat().st_size != document.size_bytes or sha256_file(path) != document.sha256:
            raise ValueError('Corpus document size or hash mismatch')
        expires = document.authorization_expires_at
        if expires is not None and expires.astimezone(timezone.utc) <= now:
            raise ValueError('Corpus authorization has expired')
        if path.suffix.lower() not in {'.md', '.txt', '.pdf'}:
            raise ValueError('Corpus contains an unsupported document type')
        document_paths[document.sha256] = path
        try:
            _, pages = parse(path.name, path.read_bytes())
            chunks_by_document[document.sha256] = tuple(item['text'] for item in chunks(pages))
        except InputError as exc:
            raise ValueError('Corpus document cannot be parsed with the current parser') from exc
    split_by_source: dict[str, str] = {}
    split_by_template: dict[str, str] = {}
    reviewer_ids = set()
    quotes_by_document: dict[str, set[str]] = {}
    question_fingerprints: set[str] = set()
    for case in cases:
        if _is_placeholder(case.question):
            raise ValueError('Dataset question contains a placeholder pattern')
        question_fingerprint = ' '.join(case.question.casefold().split())
        if question_fingerprint in question_fingerprints:
            raise ValueError('Dataset questions must be unique after normalization')
        question_fingerprints.add(question_fingerprint)
        for mapping, group_key in ((split_by_source, case.source_group), (split_by_template, case.template_group)):
            previous = mapping.setdefault(group_key, case.split)
            if previous != case.split:
                raise ValueError('Source or template group leaks across dev and holdout')
        label = labels_by_case[case.id]
        if any(_is_placeholder(value) for value in [*label.required_facts, *label.forbidden_claims, *label.reviewer_ids]):
            raise ValueError('Dataset label contains a placeholder pattern')
        reviewer_ids.update(label.reviewer_ids)
        if case.answerability == 'answerable':
            if len(label.evidence_groups) != len(label.required_facts):
                raise ValueError('Answerable cases require one evidence group per required fact')
        elif label.evidence_groups:
            raise ValueError('Unanswerable cases cannot contain gold evidence groups')
        for evidence_group in label.evidence_groups:
            for passage in evidence_group:
                if passage.document_sha256 not in documents_by_hash:
                    raise ValueError('Gold evidence references a document outside the authorized corpus')
                if not any(passage.quote.strip() in text for text in chunks_by_document[passage.document_sha256]):
                    raise ValueError('Gold quote is not present in a source chunk')
                quotes_by_document.setdefault(passage.document_sha256, set()).add(passage.quote.strip())
        if formal:
            if len(label.reviewer_ids) < 2:
                raise ValueError('Formal datasets require two distinct reviewers per case')
            if label.dispute_status == 'unresolved':
                raise ValueError('Formal datasets cannot contain unresolved disputes')
    for digest, quotes in quotes_by_document.items():
        path = document_paths[digest]
        if path.suffix.lower() in {'.md', '.txt'}:
            text_content = path.read_text(encoding='utf-8-sig')
            if any(quote not in text_content for quote in quotes):
                raise ValueError('Gold quote is absent from its text document')
    splits = {item.split for item in cases}
    if formal and (len(cases) < 40 or splits != {'dev', 'holdout'} or not manifest.sealed_holdout):
        raise ValueError('Formal release data requires 40 cases, both splits and a sealed holdout')
    holdout_cases = [item for item in cases if item.split == 'holdout']
    holdout_template_counts = {
        group: sum(item.template_group == group for item in holdout_cases)
        for group in {item.template_group for item in holdout_cases}
    }
    if formal and any(count * 5 > len(holdout_cases) for count in holdout_template_counts.values()):
        raise ValueError('Formal holdout template groups cannot exceed 20% of cases')
    frozen_payload = {
        'format_version': manifest.format_version,
        'dataset_id': manifest.dataset_id,
        'version': manifest.version,
        'authoring_status': manifest.authoring_status,
        'semantic_review_status': manifest.semantic_review_status,
        'sealed_holdout': manifest.sealed_holdout,
        'split_policy': manifest.split_policy,
        **observed_hashes,
    }
    frozen_hash = hashlib.sha256(json.dumps(frozen_payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {
        'status': 'passed',
        'formal': formal,
        'dataset_id': manifest.dataset_id,
        'version': manifest.version,
        'dataset_hash': frozen_hash,
        'corpus_documents': len(corpus.documents),
        'cases': len(cases),
        'dev_cases': sum(item.split == 'dev' for item in cases),
        'holdout_cases': sum(item.split == 'holdout' for item in cases),
        'reviewers': len(reviewer_ids),
        'unresolved_disputes': sum(item.dispute_status == 'unresolved' for item in labels),
        'authoring_status': manifest.authoring_status,
        'semantic_review_status': manifest.semantic_review_status,
        'sealed_holdout': manifest.sealed_holdout,
    }
