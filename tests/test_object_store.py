import io
from pathlib import PureWindowsPath
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.storage.object_store import ObjectStore, StorageError, comparable_path

DRIVE = 'C:'

@pytest.mark.parametrize('root,extended,sibling', [
    (f'{DRIVE}/objects', f'//?/{DRIVE}/objects/sha256/ab/file', f'//?/{DRIVE}/objects-other/file'),
    ('//server/share/objects', '//?/UNC/server/share/objects/sha256/ab/file', '//?/UNC/server/share/other/file'),
])
def test_windows_namespace_alias_preserves_containment(root, extended, sibling):
    canonical = comparable_path(PureWindowsPath(root))
    assert comparable_path(PureWindowsPath(extended)).is_relative_to(canonical)
    assert not comparable_path(PureWindowsPath(sibling)).is_relative_to(canonical)
    assert not comparable_path(PureWindowsPath('//?/GLOBALROOT/device/file')).is_relative_to(canonical)


def test_atomic_deduplication_under_concurrent_uploads(tmp_path):
    store = ObjectStore(tmp_path / 'objects')
    with ThreadPoolExecutor(max_workers=8) as executor:
        items = list(executor.map(lambda _: store.put(io.BytesIO(b'original content')), range(16)))
    assert len({item.storage_key for item in items}) == 1
    assert len(list((store.root / 'sha256').glob('*/*'))) == 1
    assert list(store.temporary.iterdir()) == []
    store.verify(items[0])


@pytest.mark.parametrize('key', ['../outside', '/etc/passwd', 'CON', 'a' * 400,
                                    'sha256/ab/' + 'c' * 64, 'sha256/aa/' + 'a' * 64 + '/../../outside'])
def test_untrusted_names_cannot_be_storage_paths(tmp_path, key):
    store = ObjectStore(tmp_path / 'objects')
    with pytest.raises(StorageError):
        store.path_for(key)


def test_failed_upload_cleans_only_its_scratch_file(tmp_path):
    store = ObjectStore(tmp_path / 'objects')
    original = store.put(io.BytesIO(b'keep'))
    with pytest.raises(StorageError, match='OBJECT_TOO_LARGE'):
        store.put(io.BytesIO(b'too large'), max_bytes=3)
    store.verify(original)
    assert list(store.temporary.iterdir()) == []


def test_corrupt_existing_object_is_never_overwritten(tmp_path):
    store = ObjectStore(tmp_path / 'objects')
    item = store.put(io.BytesIO(b'original'))
    store.path_for(item.storage_key).write_bytes(b'corrupted')
    with pytest.raises(StorageError, match='OBJECT_CORRUPT'):
        store.put(io.BytesIO(b'original'))
    assert store.path_for(item.storage_key).read_bytes() == b'corrupted'


def test_orphan_report_is_delayed_and_read_only(tmp_path):
    store = ObjectStore(tmp_path / 'objects')
    used = store.put(io.BytesIO(b'used'))
    orphan = store.put(io.BytesIO(b'orphan after DB rollback'))
    assert store.orphan_candidates([used.storage_key]) == []
    assert store.orphan_candidates([used.storage_key], minimum_age_seconds=0) == [orphan.storage_key]
    store.verify(orphan)
