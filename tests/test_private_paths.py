"""Private temp confinement and STT cleanup use synthetic bytes/fake consumers."""
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from core import config, private_paths, stt
from core.private_paths import PrivatePathError, private_temp_dir, temporary_file


def test_default_location_and_explicit_instance_do_not_change_environment(tmp_path, monkeypatch):
    primary, alternate = tmp_path / 'primary', tmp_path / 'alternate'
    monkeypatch.setattr(config, 'ROOT', primary)
    before = dict(os.environ)
    assert private_temp_dir() == primary / 'data' / 'tmp'
    assert private_temp_dir(alternate) == alternate / 'data' / 'tmp'
    assert config.ROOT == primary and dict(os.environ) == before
    if os.name != 'nt':
        assert private_temp_dir().stat().st_mode & 0o077 == 0


def test_context_yields_reopenable_file_and_cleans_its_own_file_only(tmp_path):
    folder = private_temp_dir(tmp_path)
    retained = folder / 'existing-recording.webm'
    retained.write_bytes(b'Existing private data must remain.')
    with temporary_file(b'Synthetic recording', root=tmp_path, suffix='.webm') as path:
        assert path.parent == folder and path.suffix == '.webm'
        assert path.read_bytes() == b'Synthetic recording'
        with path.open('rb') as consumer:
            assert consumer.read() == b'Synthetic recording'
    assert not path.exists()
    assert retained.read_bytes() == b'Existing private data must remain.'


def test_consumer_exception_and_empty_context_both_cleanup(tmp_path):
    with pytest.raises(RuntimeError, match='Synthetic consumer failure'):
        with temporary_file(b'Partial work', root=tmp_path) as path:
            raise RuntimeError('Synthetic consumer failure')
    assert not path.exists()
    with temporary_file(root=tmp_path) as empty:
        assert empty.read_bytes() == b''
    assert not empty.exists()


def test_partial_write_error_removes_created_plaintext(tmp_path, monkeypatch):
    write = os.write
    def fail_write(fd, data):
        write(fd, data[:4])
        raise OSError('Synthetic disk write failure')
    monkeypatch.setattr(private_paths.os, 'write', fail_write)
    with pytest.raises(OSError, match='Synthetic disk write failure'):
        with temporary_file(b'Private synthetic recording', root=tmp_path):
            pytest.fail('A failed writer cannot yield a usable file.')
    assert list((tmp_path / 'data' / 'tmp').iterdir()) == []


def test_short_writes_complete_and_zero_write_fails_cleanly(tmp_path, monkeypatch):
    write = os.write
    monkeypatch.setattr(private_paths.os, 'write', lambda fd, data: write(fd, data[:2]))
    with temporary_file(b'123456789', root=tmp_path) as path:
        assert path.read_bytes() == b'123456789'
    monkeypatch.setattr(private_paths.os, 'write', lambda fd, data: 0)
    with pytest.raises(OSError, match='progress'):
        with temporary_file(b'Unwritten', root=tmp_path):
            pass
    assert list((tmp_path / 'data' / 'tmp').iterdir()) == []


@pytest.mark.parametrize('component', ['data', 'tmp'])
def test_non_directory_collision_is_not_removed_or_replaced(tmp_path, component):
    target = tmp_path / 'data'
    if component == 'tmp':
        target.mkdir()
        target /= 'tmp'
    target.write_bytes(b'Existing file')
    with pytest.raises((PrivatePathError, FileExistsError)):
        private_temp_dir(tmp_path)
    assert target.read_bytes() == b'Existing file'


@pytest.mark.parametrize('component', ['instance', 'data', 'tmp'])
def test_directory_symlink_escape_is_rejected_before_writing(tmp_path, component):
    outside = tmp_path / 'outside'
    outside.mkdir()
    instance = tmp_path / 'instance'
    target = instance
    if component != 'instance':
        instance.mkdir()
        target = instance / 'data'
    if component == 'tmp':
        target.mkdir()
        target /= 'tmp'
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('The host does not permit creating a directory symlink.')
    with pytest.raises(PrivatePathError):
        with temporary_file(b'Must remain private', root=instance):
            pass
    assert list(outside.iterdir()) == []


def test_reparse_point_attributes_are_rejected_on_any_host(tmp_path, monkeypatch):
    private_temp_dir(tmp_path)
    lstat = Path.lstat
    def reparse(path, *args, **kwargs):
        result = lstat(path, *args, **kwargs)
        if path == tmp_path / 'data' / 'tmp':
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result
    monkeypatch.setattr(Path, 'lstat', reparse)
    with pytest.raises(PrivatePathError, match='reparse'):
        with temporary_file(b'Not written', root=tmp_path):
            pass
    assert list((tmp_path / 'data' / 'tmp').iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory junctions use reparse attributes.')
def test_windows_junction_escape_is_rejected_before_writing(tmp_path):
    instance, outside = tmp_path / 'instance', tmp_path / 'outside'
    instance.mkdir()
    outside.mkdir()
    target = instance / 'data'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(outside)], capture_output=True, check=False)
    if result.returncode:
        pytest.skip('The host does not permit creating a directory junction.')
    with pytest.raises(PrivatePathError, match='reparse'):
        with temporary_file(b'Not written through junction', root=instance):
            pass
    assert list(outside.iterdir()) == []


def test_existing_filename_collision_never_truncates_or_deletes(tmp_path, monkeypatch):
    folder = private_temp_dir(tmp_path)
    identifier = 'a' * 32
    retained = folder / ('altera-' + identifier + '.webm')
    retained.write_bytes(b'Original')
    monkeypatch.setattr(private_paths.uuid, 'uuid4', lambda: SimpleNamespace(hex=identifier))
    with pytest.raises(PrivatePathError, match='unique'):
        with temporary_file(b'Replacement', root=tmp_path, suffix='.webm'):
            pass
    assert retained.read_bytes() == b'Original'


def test_replaced_owned_file_is_left_untouched(tmp_path):
    with pytest.raises(PrivatePathError, match='replacement'):
        with temporary_file(b'Owned', root=tmp_path) as path:
            original = path.with_suffix('.preserved')
            path.rename(original)
            path.write_bytes(b'Unrelated replacement')
    assert path.read_bytes() == b'Unrelated replacement'
    assert original.read_bytes() == b'Owned'


@pytest.mark.parametrize(('field', 'value'), [('prefix', '../escape'), ('prefix', 'a/b'), ('suffix', '/../outside'),
                                           ('suffix', '.webm:stream'), ('suffix', '\\recording')])
def test_filename_affixes_cannot_escape_or_create_alternate_streams(tmp_path, field, value):
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        with temporary_file(b'Not written', root=tmp_path, **{field: value}):
            pass
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize('offline_only', [True, False])
def test_stt_success_uses_instance_file_and_cleans_before_return(tmp_path, monkeypatch, offline_only):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    paths = []
    def transcribe(path, model_size, language, **options):
        path = Path(path)
        paths.append(path)
        assert path.parent == tmp_path / 'data' / 'tmp'
        assert path.read_bytes() == b'Synthetic audio'
        assert model_size == 'fake-model' and language == 'en'
        assert options == ({'offline_only': True} if offline_only else {})
        return {'text': 'Synthetic transcript', 'language': 'en'}
    monkeypatch.setattr(stt, 'transcribe_audio', transcribe)
    result = stt.transcribe_from_bytes(b'Synthetic audio', 'fake-model', 'en', offline_only=offline_only)
    assert result['text'] == 'Synthetic transcript'
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.parametrize('failure', ['raises', 'returns_error'])
def test_stt_transcription_failure_cleans_recording(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    paths = []
    def transcribe(path, *args, **kwargs):
        paths.append(Path(path))
        assert paths[-1].exists()
        if failure == 'raises':
            raise RuntimeError('Synthetic model failure with private details')
        return {'text': '', 'error': stt.TRANSCRIPTION_FAILED}
    monkeypatch.setattr(stt, 'transcribe_audio', transcribe)
    result = stt.transcribe_from_bytes(b'Private synthetic recording')
    assert result == {'text': '', 'error': stt.TRANSCRIPTION_FAILED}
    assert not paths[0].exists()


def test_stt_write_failure_never_calls_model_and_cleans_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(stt, 'transcribe_audio', lambda *args, **kwargs: pytest.fail('Writing failed before inference.'))
    write = os.write
    def fail_write(fd, data):
        write(fd, data[:3])
        raise OSError('Synthetic private write failure')
    monkeypatch.setattr(private_paths.os, 'write', fail_write)
    assert stt.transcribe_from_bytes(b'Synthetic recording') == {'text': '', 'error': stt.TRANSCRIPTION_FAILED}
    assert list((tmp_path / 'data' / 'tmp').iterdir()) == []


def test_stt_unsafe_temp_path_never_uses_system_temp_or_model(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    target = tmp_path / 'data'
    target.write_bytes(b'Existing collision')
    monkeypatch.setattr(stt, 'transcribe_audio', lambda *args, **kwargs: pytest.fail('Unsafe storage cannot reach inference.'))
    assert stt.transcribe_from_bytes(b'Synthetic recording') == {'text': '', 'error': stt.TRANSCRIPTION_FAILED}
    assert target.read_bytes() == b'Existing collision'


def test_stt_cleanup_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(stt, 'transcribe_audio', lambda *args, **kwargs: {'text': 'Synthetic transcript'})
    unlink = Path.unlink
    def fail_owned(path, *args, **kwargs):
        if path.parent == tmp_path / 'data' / 'tmp':
            raise PermissionError('Synthetic busy temporary recording')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_owned)
    assert stt.transcribe_from_bytes(b'Synthetic recording') == {'text': '', 'error': stt.TRANSCRIPTION_FAILED}
    retained = list((tmp_path / 'data' / 'tmp').iterdir())
    assert len(retained) == 1 and retained[0].read_bytes() == b'Synthetic recording'
    # Explicitly remove this test-created failure artifact after restoring unlink.
    monkeypatch.setattr(Path, 'unlink', unlink)
    retained[0].unlink()
