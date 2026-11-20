"""The suite cannot discover and unload an installed personal model service."""
from core import llm


def test_fake_picture_pipeline_cannot_discover_installed_lmstudio_control(monkeypatch, tmp_path):
    # Even an available CLI directory is ineligible to default test discovery.
    binary = tmp_path / 'lms.exe'
    binary.write_bytes(b'synthetic unexecutable CLI marker')
    monkeypatch.setenv('PATH', str(tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError('A test attempted actual provider process control')
    monkeypatch.setattr(llm.subprocess, 'run', forbidden)
    assert llm.shutil.which('lms') is None
    assert llm.shutil.which('lms.exe') is None
    assert llm.shutil.which(b'lms') is None
    assert llm._default_lms_path() is None
    assert llm.unload_models() is False
