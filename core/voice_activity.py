"""Bounded, local CPU speech activity checks; no transcription or audio retention."""
import importlib.util
from pathlib import Path
import threading


SAMPLE_RATE = 16000
MIN_SAMPLES = 512
MAX_SAMPLES = SAMPLE_RATE
MAX_PCM_BYTES = MAX_SAMPLES * 2
SPEECH_THRESHOLD = .6

# Provenance: faster-whisper's MIT-licensed vad.py (SYSTRAN, 2023) adapts
# https://github.com/snakers4/silero-vad and ships assets/silero_vad_v6.onnx.
# The inspected faster-whisper 1.2.1 asset is 1,245,151 bytes, SHA256
# 4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2.
# Reuse that installed package's wrapper/asset; Altera neither redistributes nor
# downloads weights. Its wrapper explicitly selects CPUExecutionProvider,
# one intra/inter-op thread, and fresh recurrent state for each short frame.
_work_lock = threading.Lock()
_model = None
_load_failed = False


class VoiceActivityError(RuntimeError):
    def __init__(self, message, code='voice_activity_unavailable', status=503):
        super().__init__(message)
        self.code, self.status = code, status


def _installed_asset():
    """Check installed files/dependencies without importing or running any model."""
    try:
        spec = importlib.util.find_spec('faster_whisper')
        if spec is None or not spec.submodule_search_locations:
            return None
        if any(importlib.util.find_spec(name) is None for name in ('numpy', 'onnxruntime')):
            return None
        path = Path(next(iter(spec.submodule_search_locations))) / 'assets' / 'silero_vad_v6.onnx'
        return path if path.is_file() else None
    except (ImportError, OSError, TypeError, ValueError):
        return None


def availability():
    """Installed-only probe: availability does not claim that inference was tested."""
    return {'available': not _load_failed and _installed_asset() is not None,
            'loaded': _model is not None, 'engine': 'silero-vad-v6', 'processing': 'local-cpu',
            'sample_rate': SAMPLE_RATE, 'min_samples': MIN_SAMPLES, 'max_samples': MAX_SAMPLES}


def _load_model(asset):
    # Importing the wrapper constructs no WhisperModel and makes no network call.
    from faster_whisper.vad import SileroVADModel
    return SileroVADModel(str(asset))


def detect_pcm16(audio):
    """Classify at most one second of signed little-endian, mono PCM at 16 kHz.

    One request owns model load/inference; other requests fail immediately rather
    than queue audio or allocate workers. No audio is written, logged or cached.
    """
    global _model, _load_failed
    if not isinstance(audio, bytes) or len(audio) % 2 or not MIN_SAMPLES * 2 <= len(audio) <= MAX_PCM_BYTES:
        raise VoiceActivityError('Send 512–16000 samples of signed 16-bit mono PCM.', 'invalid_voice_activity', 400)
    if not _work_lock.acquire(blocking=False):
        raise VoiceActivityError('A speech activity check is already running. Try the next frame.', 'voice_activity_busy', 409)
    try:
        if _load_failed:
            raise VoiceActivityError('Local speech activity detection is unavailable. Use the microphone button.')
        asset = _installed_asset()
        if asset is None:
            raise VoiceActivityError('The installed local speech activity model is unavailable. Use the microphone button.')
        try:
            import numpy as np
            if _model is None:
                _model = _load_model(asset)
            samples = np.frombuffer(audio, dtype='<i2').astype(np.float32) / 32768.0
            # The installed v6 wrapper consumes batches of 512 samples; at most
            # 32 batches are possible. Padding is local to this request.
            padded = np.pad(samples, (0, (-len(samples)) % 512))
            probabilities = np.asarray(_model(padded), dtype=np.float64).reshape(-1)
            if (len(probabilities) != len(padded) // 512 or not np.isfinite(probabilities).all()
                    or (probabilities < 0).any() or (probabilities > 1).any()):
                raise ValueError('Invalid VAD result')
            voiced = probabilities >= SPEECH_THRESHOLD
            # Require adjacent positive 32 ms windows when the caller supplied
            # enough audio; the browser separately requires two positive frames.
            speech = bool(voiced[0]) if len(voiced) == 1 else bool((voiced[:-1] & voiced[1:]).any())
            return {'available': True, 'speech': speech, 'probability': float(probabilities.max()),
                    'sample_rate': SAMPLE_RATE, 'samples': len(samples)}
        except Exception:
            # Do not expose native exceptions, installed paths or audio. A broken
            # runtime is not reloaded on every polling request; restart retries it.
            _load_failed = True
            _model = None
            raise VoiceActivityError('Local speech activity detection failed. Use the microphone button.') from None
    finally:
        _work_lock.release()
