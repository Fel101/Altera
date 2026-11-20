"""Storage readiness uses fake paths/processes; no real BitLocker call occurs."""
import io
import json
import subprocess
from types import SimpleNamespace as NS

import pytest

from scripts import check_private_storage as storage

POWERSHELL = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'


def volume(drive, **changes):
    return {'MountPoint': drive, 'VolumeStatus': 'FullyEncrypted', 'ProtectionStatus': 'On',
            'LockStatus': 'Unlocked', 'EncryptionPercentage': 100, **changes}


class Processes:
    def __init__(self, changes=None, *, stdout=None, stderr='', code=0, exception=None):
        self.changes, self.stdout, self.stderr, self.code, self.exception = changes or {}, stdout, stderr, code, exception
        self.calls = []
    def __call__(self, command, **options):
        self.calls.append((command, options))
        if self.exception:
            raise self.exception
        drives = json.loads(options['input'])['drives']
        rows = [{'drive': drive, 'volume': volume(drive, **self.changes.get(drive, {})), 'error': None} for drive in drives]
        return NS(stdout=self.stdout if self.stdout is not None else json.dumps({'schema': 1, 'volumes': rows}), stderr=self.stderr, returncode=self.code)


def inspect(processes, home=r'D:\Altera', additional=(), **options):
    return storage.inspect_storage(home, additional, platform='nt', powershell=lambda: POWERSHELL,
                                   resolver=lambda path: path, runner=processes, **options)


def test_scoped_drives_include_os_resolved_instance_and_repeatable_extras_without_reading_contents():
    processes = Processes()
    result = inspect(processes, additional=[r'E:\Browser profile', r'F:\Downloads', r'D:\cache'])
    assert result['scope']['queried_drives'] == ['C:', 'D:', 'E:', 'F:']
    assert result['scope']['additional_path_count'] == 3
    assert result['status'] == 'QUERIED_DRIVES_PROTECTED'
    assert result['whole_app_coverage_verified'] is False
    assert len(processes.calls) == 1
    command, options = processes.calls[0]
    assert command[0] == POWERSHELL and command[-1] == storage.POWERSHELL_SCRIPT
    assert '-NoProfile' in command and '-NonInteractive' in command
    assert json.loads(options['input']) == {'drives': ['C:', 'D:', 'E:', 'F:']}
    assert options['timeout'] == 15 and options['max_output'] == 65536
    assert 'Get-BitLockerVolume' in command[-1] and '-ErrorAction Stop' in command[-1]
    assert 'Select-Object MountPoint,VolumeStatus,ProtectionStatus,LockStatus,EncryptionPercentage' in command[-1]
    assert 'KeyProtector' not in command[-1]
    assert r'E:\Browser profile' not in str(command)


def test_junction_resolution_changes_queried_drive_not_the_command():
    processes = Processes()
    result = storage.inspect_storage(r'C:\mounted-instance', [r'C:\browser-link'], platform='nt', powershell=lambda: POWERSHELL,
        resolver=lambda path: r'D:\real-instance' if path.endswith('instance') else r'E:\real-browser', runner=processes)
    assert result['scope']['instance_drive'] == 'D:'
    assert result['scope']['queried_drives'] == ['C:', 'D:', 'E:']


@pytest.mark.parametrize(('changes', 'status', 'reason'), [
    ({'ProtectionStatus': 'Off'}, 'UNPROTECTED', 'protection_off'),
    ({'VolumeStatus': 'EncryptionInProgress', 'EncryptionPercentage': 75}, 'UNPROTECTED', 'not_fully_encrypted'),
    ({'VolumeStatus': 'FullyDecrypted', 'ProtectionStatus': 'Off', 'EncryptionPercentage': 0}, 'UNPROTECTED', 'protection_off'),
    ({'LockStatus': 'Locked'}, 'LOCKED', 'volume_locked'),
    ({'VolumeStatus': 'Unknown'}, 'UNKNOWN', 'unknown_volume_state'),
    ({'ProtectionStatus': 'Unknown'}, 'UNKNOWN', 'unknown_volume_state'),
    ({'LockStatus': 'Unknown'}, 'UNKNOWN', 'unknown_volume_state'),
    ({'EncryptionPercentage': 99}, 'UNKNOWN', 'inconsistent_snapshot'),
    ({'EncryptionPercentage': True}, 'UNKNOWN', 'malformed_volume'),
    ({'EncryptionPercentage': None}, 'UNKNOWN', 'malformed_volume'),
    ({'EncryptionPercentage': 101}, 'UNKNOWN', 'malformed_volume'),
    ({'ProtectionStatus': 1}, 'UNKNOWN', 'malformed_volume'),
    ({'MountPoint': 'Z:'}, 'UNKNOWN', 'mount_mismatch'),
])
def test_suspended_partial_locked_unknown_and_malformed_states_are_distinct(changes, status, reason):
    processes = Processes({'D:': changes})
    result = inspect(processes)
    item = next(row for row in result['volumes'] if row['drive'] == 'D:')
    assert item['status'] == status and item['reason'] == reason
    assert result['status'] == ('UNKNOWN' if status == 'UNKNOWN' else 'NOT_READY')


def test_exit_zero_access_denied_stderr_never_becomes_unprotected_or_ready():
    body = {'schema': 1, 'volumes': [{'drive': 'C:', 'volume': None, 'error': 'no_volume'}]}
    result = inspect(Processes(stdout=json.dumps(body), stderr='Access is denied (0x80070005). No associated volume.', code=0), home=r'C:\Altera')
    assert result['status'] == 'UNKNOWN'
    assert result['volumes'] == [{'drive': 'C:', 'status': 'UNKNOWN', 'reason': 'access_denied'}]
    assert 'associated volume' not in json.dumps(result)


def test_structured_access_denied_has_no_keys_or_raw_error_details():
    body = {'schema': 1, 'volumes': [{'drive': drive, 'volume': None, 'error': 'access_denied'} for drive in ('C:', 'D:')]}
    result = inspect(Processes(stdout=json.dumps(body)))
    assert result['status'] == 'UNKNOWN'
    assert all(row['reason'] == 'access_denied' for row in result['volumes'])


@pytest.mark.parametrize('stdout', ['not JSON', '{}', '{"schema":true,"volumes":[]}', '{"schema":1,"volumes":[]}',
    '{"schema":1,"volumes":[],"schema":1}', '{"schema":1,"volumes":{},"KeyProtector":"must not expose"}'])
def test_malformed_output_is_unknown_without_echoing_any_fields(stdout):
    result = inspect(Processes(stdout=stdout))
    assert result['status'] == 'UNKNOWN'
    assert 'must not expose' not in json.dumps(result)
    assert all(row['status'] == 'UNKNOWN' and 'evidence' not in row for row in result['volumes'])


def test_extra_sensitive_volume_fields_are_not_exposed():
    record = volume('C:') | {'KeyProtector': 'synthetic secret'}
    body = {'schema': 1, 'volumes': [{'drive': 'C:', 'volume': record, 'error': None}]}
    result = inspect(Processes(stdout=json.dumps(body)), home=r'C:\Altera')
    assert result['status'] == 'UNKNOWN'
    assert 'synthetic secret' not in json.dumps(result) and 'KeyProtector' not in json.dumps(result)


@pytest.mark.parametrize(('processes', 'reason'), [
    (Processes(stdout='x' * 65537), 'output_limit'),
    (Processes(exception=subprocess.TimeoutExpired('synthetic helper', 15)), 'timeout'),
    (Processes(exception=PermissionError('Synthetic access error')), 'query_failed'),
    (Processes(code=1), 'process_failed'),
    (Processes(stderr='Unexpected helper diagnostics'), 'stderr_output'),
])
def test_process_limits_and_errors_are_unknown(processes, reason):
    result = inspect(processes)
    assert result['status'] == 'UNKNOWN' and all(row['reason'] == reason for row in result['volumes'])


@pytest.mark.parametrize('path', [r'\\server\share\private', r'\\?\Volume{synthetic}\data', 'C:relative', r'Z$:\data', '\x00invalid'])
def test_unresolved_or_nonlocal_paths_prevent_whole_scope_success(path):
    result = inspect(Processes(), additional=[path])
    assert result['status'] == 'UNKNOWN' and result['scope_issues']
    assert path not in json.dumps(result)


def test_paths_cannot_inject_powershell_and_drive_validation_precedes_invocation():
    processes = Processes()
    injection = r'D:\data; $(Start-Process evil)'
    result = inspect(processes, additional=[injection])
    assert result['status'] == 'QUERIED_DRIVES_PROTECTED'
    assert injection not in str(processes.calls[0][0])
    assert json.loads(processes.calls[0][1]['input']) == {'drives': ['C:', 'D:']}
    with pytest.raises(storage.ProbeError):
        storage.query(['C:;Write-Host evil'], POWERSHELL, processes)
    assert len(processes.calls) == 1


def test_unsupported_os_and_missing_trusted_powershell_never_invoke_runner():
    def unexpected(*args, **kwargs):
        pytest.fail('No OS or volume process may run here')
    result = storage.inspect_storage('/synthetic/home', platform='posix', powershell=unexpected, runner=unexpected)
    assert result['status'] == 'UNSUPPORTED' and result['volumes'] == []
    def unavailable():
        raise storage.ProbeError('powershell_unavailable')
    result = storage.inspect_storage(r'C:\Altera', platform='nt', powershell=unavailable, runner=unexpected)
    assert result['status'] == 'UNKNOWN'


def test_cli_json_exit_codes_report_scoped_results_only():
    messages = []
    common = {'platform': 'nt', 'powershell': lambda: POWERSHELL, 'resolver': lambda value: value, 'output': messages.append}
    assert storage.main(['--home', r'D:\Altera', '--additional-path', r'E:\Browser', '--additional-path', r'F:\Downloads'], runner=Processes(), **common) == 0
    result = json.loads(messages[-1])
    assert result['scope']['queried_drives'] == ['C:', 'D:', 'E:', 'F:'] and result['whole_app_coverage_verified'] is False
    assert storage.main(['--home', r'D:\Altera'], runner=Processes({'D:': {'ProtectionStatus': 'Off'}}), **common) == 1
    assert storage.main(['--home', r'D:\Altera'], runner=Processes(stderr='Access is denied'), **common) == 2


def test_bounded_runner_kills_only_its_mock_helper_on_output_overflow(monkeypatch):
    class Process:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(b'x' * 100000), io.BytesIO()
            self.returncode, self.killed = 0, False
        def wait(self, timeout):
            return self.returncode
        def kill(self):
            self.killed = True
    process, calls = Process(), []
    def popen(command, **options):
        calls.append((command, options))
        return process
    monkeypatch.setattr(storage.subprocess, 'Popen', popen)
    with pytest.raises(storage.ProbeError, match='output_limit'):
        storage.run_bounded([POWERSHELL, '-Command', 'fixed synthetic script'], input='{}', max_output=64)
    assert process.killed and calls[0][1]['shell'] is False


def test_bounded_runner_timeout_kills_its_mock_helper_and_reports_unknown(monkeypatch):
    class Process:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
            self.returncode, self.waits, self.killed = 0, 0, False
        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired('synthetic', timeout)
            return 0
        def kill(self):
            self.killed = True
    process = Process()
    monkeypatch.setattr(storage.subprocess, 'Popen', lambda *args, **kwargs: process)
    with pytest.raises(storage.ProbeError, match='timeout'):
        storage.run_bounded([POWERSHELL], input='{}')
    assert process.killed
