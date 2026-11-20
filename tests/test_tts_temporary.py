"""TTS temp publication uses synthetic media and fake Edge consumers only."""
import asyncio
import errno
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest

from core import config, private_paths, tts


@pytest.fixture
def audio_world(tmp_path, monkeypatch):
    root = tmp_path / 'synthetic-instance'
    cache = root / 'data' / 'cache' / 'tts'
    cache.mkdir(parents=True)
    monkeypatch.setattr(config, 'ROOT', root)
    monkeypatch.setattr(tts, 'AUDIO_CACHE_DIR', str(cache))
    monkeypatch.setattr(tts, '_pruned', False)
    return SimpleNamespace(root=root, cache=cache, temp=root / 'data' / 'tmp')


def wav_bytes(frames, rate=16000, sample=b'\x01\x00'):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(sample * frames)
    return output.getvalue()


def clips(w):
    paths = [w.cache / 'first.wav', w.cache / 'second.wav']
    paths[0].write_bytes(wav_bytes(12))
    paths[1].write_bytes(wav_bytes(9, sample=b'\x02\x00'))
    return [str(path) for path in paths]


def assert_clean(w):
    assert not w.temp.exists() or list(w.temp.iterdir()) == []
    assert not list(w.cache.glob('*.tmp'))


def test_atomic_bytes_publish_uses_closed_private_file_and_preserves_target_until_commit(audio_world, monkeypatch):
    w = audio_world
    target, old = w.cache / 'target.wav', w.cache / 'old-unrelated.wav'
    target.write_bytes(b'Previous result')
    old.write_bytes(b'Expired unrelated cache')
    os.utime(target, (1, 1))
    os.utime(old, (1, 1))
    replace, observed = os.replace, []
    def publish(source, destination):
        source = Path(source)
        assert source.parent == w.temp and source.read_bytes() == b'Complete synthetic WAV'
        assert target.read_bytes() == b'Previous result'  # No pre-publication prune.
        observed.append(source)
        return replace(source, destination)
    monkeypatch.setattr(tts.os, 'replace', publish)
    tts._write_atomic(str(target), b'Complete synthetic WAV')
    assert target.read_bytes() == b'Complete synthetic WAV'
    assert not old.exists() and len(observed) == 1
    assert_clean(w)


def test_partial_private_write_failure_keeps_preexisting_cache_bytes(audio_world, monkeypatch):
    w = audio_world
    target = w.cache / 'previous.wav'
    target.write_bytes(b'Existing cached audio')
    os.utime(target, (1, 1))
    write = os.write
    def fail_write(fd, content):
        write(fd, content[:5])
        raise OSError('Synthetic full disk')
    monkeypatch.setattr(private_paths.os, 'write', fail_write)
    with pytest.raises(OSError, match='full disk'):
        tts._write_atomic(str(target), b'Partial synthetic output')
    assert target.read_bytes() == b'Existing cached audio'
    assert tts._pruned is False
    assert_clean(w)


@pytest.mark.parametrize('error', [PermissionError('Synthetic cache publication denied'), OSError(errno.EXDEV, 'Synthetic cross-volume cache')])
def test_failed_atomic_replace_never_falls_back_to_copy_or_overwrites_target(audio_world, monkeypatch, error):
    w = audio_world
    target = w.cache / 'previous.wav'
    target.write_bytes(b'Existing cached audio')
    def denied(source, destination):
        assert Path(source).parent == w.temp
        raise error
    monkeypatch.setattr(tts.os, 'replace', denied)
    with pytest.raises(OSError):
        tts._write_atomic(str(target), b'Unpublished synthetic output')
    assert target.read_bytes() == b'Existing cached audio'
    assert_clean(w)


def test_concat_reopens_private_wav_and_atomically_publishes_complete_frames(audio_world, monkeypatch):
    w = audio_world
    sources = clips(w)
    native_open, outputs = wave.open, []
    def inspect(path, mode):
        if mode == 'wb':
            path = Path(path)
            assert path.parent == w.temp and path.read_bytes() == b''
            outputs.append(path)
        return native_open(str(path), mode)
    monkeypatch.setattr(tts.wave, 'open', inspect)
    result = tts._concat_wavs(sources)
    assert Path(result).parent == w.cache
    with native_open(result, 'rb') as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) == (1, 2, 16000, 21)
        assert audio.readframes(21) == b'\x01\x00' * 12 + b'\x02\x00' * 9
    assert len(outputs) == 1
    monkeypatch.setattr(tts.wave, 'open', lambda *args, **kwargs: pytest.fail('A cache hit must not reopen native audio.'))
    assert tts._concat_wavs(sources) == result
    assert_clean(w)


def test_native_wav_write_failure_cleans_partial_file_and_leaves_source_clips(audio_world, monkeypatch):
    w = audio_world
    sources = clips(w)
    originals = [Path(path).read_bytes() for path in sources]
    native_write = wave.Wave_write.writeframes
    def fail_write(audio, frames):
        native_write(audio, frames)
        raise OSError('Synthetic native WAV failure')
    monkeypatch.setattr(wave.Wave_write, 'writeframes', fail_write)
    with pytest.raises(OSError, match='native WAV failure'):
        tts._concat_wavs(sources)
    assert [Path(path).read_bytes() for path in sources] == originals
    assert sorted(path.name for path in w.cache.iterdir()) == ['first.wav', 'second.wav']
    assert_clean(w)


def test_concat_replace_failure_cleans_finished_private_wav(audio_world, monkeypatch):
    w = audio_world
    sources = clips(w)
    def denied(source, target):
        assert Path(source).parent == w.temp
        with wave.open(str(source), 'rb') as audio:
            assert audio.getnframes() == 21
        raise PermissionError('Synthetic destination busy')
    monkeypatch.setattr(tts.os, 'replace', denied)
    with pytest.raises(PermissionError, match='destination busy'):
        tts._concat_wavs(sources)
    assert sorted(path.name for path in w.cache.iterdir()) == ['first.wav', 'second.wav']
    assert_clean(w)


def fake_edge(monkeypatch, save, calls):
    class Communicate:
        def __init__(self, text, voice, **options):
            calls.append((text, voice, options))
        async def save(self, path):
            return await save(Path(path))
    monkeypatch.setitem(sys.modules, 'edge_tts', SimpleNamespace(Communicate=Communicate))


def test_fake_edge_opens_closed_private_file_and_reuses_published_cache(audio_world, monkeypatch):
    w = audio_world
    calls, outputs = [], []
    async def save(path):
        assert path.parent == w.temp and path.read_bytes() == b''
        with path.open('wb') as media:
            media.write(b'Synthetic MP3 bytes')
        outputs.append(path)
    fake_edge(monkeypatch, save, calls)
    result = asyncio.run(tts.generate_speech_async('Synthetic words', 'fake-voice', rate='+5%'))
    assert Path(result).parent == w.cache and Path(result).read_bytes() == b'Synthetic MP3 bytes'
    assert asyncio.run(tts.generate_speech_async('Synthetic words', 'fake-voice', rate='+5%')) == result
    assert calls == [('Synthetic words', 'fake-voice', {'rate': '+5%'})]
    assert len(outputs) == 1
    assert_clean(w)


def test_fake_edge_failure_cleans_partial_output(audio_world, monkeypatch):
    w = audio_world
    async def failed(path):
        assert path.parent == w.temp
        path.write_bytes(b'Partial fake MP3')
        raise RuntimeError('Synthetic Edge encoder failure')
    fake_edge(monkeypatch, failed, [])
    with pytest.raises(RuntimeError, match='encoder failure'):
        asyncio.run(tts.generate_speech_async('Synthetic words'))
    assert list(w.cache.iterdir()) == []
    assert_clean(w)


def test_async_cancel_closes_consumer_and_cleans_private_recording(audio_world, monkeypatch):
    w = audio_world
    observed = []
    async def scenario():
        entered, never = asyncio.Event(), asyncio.Event()
        async def blocked(path):
            observed.append(path)
            with path.open('wb') as media:
                media.write(b'Partial synthetic MP3')
                media.flush()
                entered.set()
                await never.wait()
        fake_edge(monkeypatch, blocked, [])
        task = asyncio.create_task(tts.generate_speech_async('Synthetic canceled words'))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert observed[0].read_bytes() == b'Partial synthetic MP3'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert len(observed) == 1 and not observed[0].exists()
    assert list(w.cache.iterdir()) == []
    assert_clean(w)


def test_edge_replace_failure_preserves_another_completed_cache_writer(audio_world, monkeypatch):
    w = audio_world
    destination = Path(tts._cache_path('edge', tts.DEFAULT_VOICE, '+0%', 'Synthetic words', '.mp3'))
    async def save(path):
        path.write_bytes(b'Unpublished synthetic MP3')
        destination.write_bytes(b'Other completed cached audio')
    fake_edge(monkeypatch, save, [])
    def denied(source, target):
        assert Path(source).parent == w.temp
        raise PermissionError('Synthetic native publication failure')
    monkeypatch.setattr(tts.os, 'replace', denied)
    with pytest.raises(PermissionError, match='publication failure'):
        asyncio.run(tts.generate_speech_async('Synthetic words'))
    assert destination.read_bytes() == b'Other completed cached audio'
    assert_clean(w)
