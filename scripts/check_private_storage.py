"""Read-only, scoped Windows volume readiness; importing performs no checks."""
from __future__ import annotations

import argparse
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace

TIMEOUT_SECONDS = 15
MAX_OUTPUT_BYTES = 65536
CODE_ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'MountPoint', 'VolumeStatus', 'ProtectionStatus', 'LockStatus', 'EncryptionPercentage'}
VOLUME_STATES = {'FullyEncrypted', 'FullyDecrypted', 'EncryptionInProgress', 'DecryptionInProgress', 'EncryptionPaused', 'DecryptionPaused', 'Unknown'}
ACCESS_DENIED = re.compile(r'access\s+is\s+denied|accessdenied|unauthorizedaccess|permissiondenied|0x80070005|-2147024891', re.I)
POWERSHELL_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $drives = @($request.drives)
    if ($drives.Count -lt 1 -or $drives.Count -gt 26) { throw 'Invalid drive request' }
    foreach ($drive in $drives) {
        if ($drive -isnot [string] -or $drive -cnotmatch '^[A-Z]:$') { throw 'Invalid drive request' }
    }
    if (@($drives | Select-Object -Unique).Count -ne $drives.Count) { throw 'Duplicate drive request' }
    $module = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\Modules\BitLocker\BitLocker.psd1'
    Import-Module -Name $module -ErrorAction Stop
} catch {
    @{schema=1;error='bitlocker_unavailable';volumes=@()} | ConvertTo-Json -Compress -Depth 4
    exit 1
}
$results = @(
    foreach ($drive in $drives) {
        try {
            $found = @(BitLocker\Get-BitLockerVolume -MountPoint $drive -ErrorAction Stop |
                Select-Object MountPoint,VolumeStatus,ProtectionStatus,LockStatus,EncryptionPercentage)
            if ($found.Count -ne 1) {
                @{drive=$drive;error='no_volume';volume=$null}
            } else {
                $v = $found[0]
                @{drive=$drive;error=$null;volume=@{
                    MountPoint=[string]$v.MountPoint;VolumeStatus=[string]$v.VolumeStatus;
                    ProtectionStatus=[string]$v.ProtectionStatus;LockStatus=[string]$v.LockStatus;
                    EncryptionPercentage=$v.EncryptionPercentage}}
            }
        } catch {
            $denied = $_.CategoryInfo.Category -eq 'PermissionDenied' -or
                $_.Exception -is [UnauthorizedAccessException] -or
                ($_.FullyQualifiedErrorId -match 'AccessDenied|UnauthorizedAccess|PermissionDenied|80070005') -or
                ($_.Exception.Message -match 'Access is denied|0x80070005|-2147024891')
            @{drive=$drive;error=$(if ($denied) {'access_denied'} else {'query_failed'});volume=$null}
        }
    }
)
@{schema=1;volumes=$results} | ConvertTo-Json -Compress -Depth 4
'''


class ProbeError(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def trusted_powershell():
    """Resolve the OS executable through Windows, never PATH or user settings."""
    if os.name != 'nt':
        raise ProbeError('unsupported_os')
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise ProbeError('system_directory_unavailable')
    executable = Path(buffer.value) / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    if not executable.is_file():
        raise ProbeError('powershell_unavailable')
    return str(executable)


def validated_drive(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z]:', value):
        raise ProbeError('unsupported_drive')
    return value.upper()


def path_drive(value, resolver=None):
    """Resolve existing junctions before reducing a local path to a drive letter."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or '\x00' in raw or len(raw) > 32768:
        raise ProbeError('unsupported_path')
    drive, _ = ntpath.splitdrive(raw)
    if drive and not ntpath.isabs(raw):
        raise ProbeError('drive_relative_path')
    try:
        resolved = str(resolver(raw) if resolver else Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        raise ProbeError('path_unavailable') from None
    if resolved.startswith('\\\\?\\'):
        resolved = resolved[4:]
    if not ntpath.isabs(resolved):
        raise ProbeError('unsupported_path')
    return validated_drive(ntpath.splitdrive(resolved)[0])


def run_bounded(command, *, input, timeout=TIMEOUT_SECONDS, max_output=MAX_OUTPUT_BYTES):
    """Bound both process lifetime and accumulated pipe bytes while reading."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    buffers, total, lock, overflow, read_error = [bytearray(), bytearray()], [0], threading.Lock(), threading.Event(), threading.Event()
    def read(stream, buffer):
        try:
            while True:
                block = stream.read(4096)
                if not block:
                    break
                with lock:
                    allowed = max(0, max_output - total[0])
                    buffer.extend(block[:allowed])
                    total[0] += len(block)
                    too_large = total[0] > max_output
                if too_large:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
        except (OSError, ValueError):
            read_error.set()
        finally:
            try:
                stream.close()
            except OSError:
                read_error.set()
    workers = [threading.Thread(target=read, args=(stream, buffer), daemon=True)
               for stream, buffer in zip((process.stdout, process.stderr), buffers)]
    for worker in workers:
        worker.start()
    timed_out = False
    try:
        process.stdin.write(input.encode('utf-8'))
        process.stdin.close()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait(timeout=2)
    except (OSError, ValueError):
        process.kill()
        process.wait(timeout=2)
        raise ProbeError('process_failed') from None
    finally:
        for worker in workers:
            worker.join(timeout=1)
    if timed_out:
        raise ProbeError('timeout')
    if overflow.is_set():
        raise ProbeError('output_limit')
    if read_error.is_set() or any(worker.is_alive() for worker in workers):
        raise ProbeError('output_incomplete')
    return SimpleNamespace(returncode=process.returncode, stdout=bytes(buffers[0]), stderr=bytes(buffers[1]))


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8-sig', errors='strict')
    if isinstance(value, str):
        return value
    raise ProbeError('malformed_output')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate response field')
        result[key] = value
    return result


def _unknown(drives, reason):
    return [{'drive': drive, 'status': 'UNKNOWN', 'reason': reason} for drive in drives]


def classify(drive, volume):
    if not isinstance(volume, dict) or set(volume) != FIELDS:
        return {'drive': drive, 'status': 'UNKNOWN', 'reason': 'malformed_volume'}
    mount = volume['MountPoint']
    if not isinstance(mount, str) or mount.rstrip('\\/').upper() != drive:
        return {'drive': drive, 'status': 'UNKNOWN', 'reason': 'mount_mismatch'}
    state, protection, locked, percent = (volume[name] for name in ('VolumeStatus', 'ProtectionStatus', 'LockStatus', 'EncryptionPercentage'))
    if (not isinstance(state, str) or state not in VOLUME_STATES or protection not in ('On', 'Off', 'Unknown')
            or locked not in ('Locked', 'Unlocked', 'Unknown') or type(percent) is not int or not 0 <= percent <= 100):
        return {'drive': drive, 'status': 'UNKNOWN', 'reason': 'malformed_volume'}
    result = {'drive': drive, 'evidence': {**volume, 'MountPoint': drive}}
    if state == 'Unknown' or protection == 'Unknown' or locked == 'Unknown':
        return {**result, 'status': 'UNKNOWN', 'reason': 'unknown_volume_state'}
    if locked == 'Locked':
        return {**result, 'status': 'LOCKED', 'reason': 'volume_locked'}
    if (state == 'FullyEncrypted' and percent != 100) or (state == 'FullyDecrypted' and percent != 0):
        return {**result, 'status': 'UNKNOWN', 'reason': 'inconsistent_snapshot'}
    if state == 'FullyEncrypted' and protection == 'On' and percent == 100:
        return {**result, 'status': 'PROTECTED', 'reason': 'fully_encrypted_protection_on_unlocked'}
    return {**result, 'status': 'UNPROTECTED', 'reason': 'protection_off' if protection == 'Off' else 'not_fully_encrypted'}


def query(drives, executable, runner=None):
    drives = [validated_drive(drive) for drive in drives]
    if not drives or len(drives) > 26 or len(set(drives)) != len(drives):
        raise ProbeError('invalid_drive_request')
    runner = runner or run_bounded
    try:
        result = runner([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', POWERSHELL_SCRIPT],
                        input=json.dumps({'drives': drives}), timeout=TIMEOUT_SECONDS, max_output=MAX_OUTPUT_BYTES)
        stdout, stderr = _text(result.stdout), _text(result.stderr)
        if len(stdout.encode('utf-8')) + len(stderr.encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise ProbeError('output_limit')
        if stderr.strip():
            return _unknown(drives, 'access_denied' if ACCESS_DENIED.search(stderr) else 'stderr_output')
        value = json.loads(stdout, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1 or set(value) - {'schema', 'volumes', 'error'}
                or not isinstance(value.get('volumes'), list)):
            raise ProbeError('malformed_output')
        if value.get('error'):
            return _unknown(drives, 'bitlocker_unavailable')
        if result.returncode != 0:
            return _unknown(drives, 'process_failed')
        records = value['volumes']
        if len(records) != len(drives) or any(not isinstance(row, dict) or set(row) != {'drive', 'volume', 'error'} for row in records):
            raise ProbeError('malformed_output')
        mapped = {row['drive']: row for row in records if isinstance(row['drive'], str)}
        if set(mapped) != set(drives) or len(mapped) != len(records):
            raise ProbeError('malformed_output')
        return [classify(drive, mapped[drive]['volume']) if mapped[drive]['error'] is None else
                {'drive': drive, 'status': 'UNKNOWN', 'reason': mapped[drive]['error'] if mapped[drive]['error'] in ('access_denied', 'query_failed', 'no_volume') else 'query_failed'}
                for drive in drives]
    except (ProbeError, subprocess.TimeoutExpired, OSError, ValueError, TypeError, AttributeError) as error:
        reason = error.reason if isinstance(error, ProbeError) else 'timeout' if isinstance(error, subprocess.TimeoutExpired) else 'query_failed'
        return _unknown(drives, reason)


def inspect_storage(home, additional_paths=(), *, platform=None, system_drive=None, resolver=None, powershell=None, runner=None):
    platform = os.name if platform is None else platform
    result = {'schema_version': 1, 'whole_app_coverage_verified': False,
              'scope': {'queried_drives': [], 'additional_path_count': len(additional_paths)}, 'volumes': [], 'scope_issues': []}
    if platform != 'nt':
        return {**result, 'status': 'UNSUPPORTED', 'reason': 'windows_bitlocker_only'}
    try:
        executable = powershell() if powershell else trusted_powershell()
        os_drive = validated_drive(system_drive) if system_drive is not None else validated_drive(ntpath.splitdrive(executable)[0])
    except (ProbeError, OSError, ValueError):
        return {**result, 'status': 'UNKNOWN', 'reason': 'trusted_powershell_unavailable'}
    drives = {os_drive}
    result['scope']['os_drive'] = os_drive
    for index, value in enumerate([home, *additional_paths]):
        try:
            drive = path_drive(value, resolver)
            drives.add(drive)
            if index == 0:
                result['scope']['instance_drive'] = drive
        except (ProbeError, OSError, TypeError, ValueError) as error:
            result['scope_issues'].append({'input': 'home' if index == 0 else 'additional_path', 'index': index,
                                           'status': 'UNKNOWN', 'reason': error.reason if isinstance(error, ProbeError) else 'path_unavailable'})
    ordered = sorted(drives)
    result['scope']['queried_drives'] = ordered
    result['volumes'] = query(ordered, executable, runner)
    states = [volume['status'] for volume in result['volumes']]
    result['status'] = ('UNKNOWN' if result['scope_issues'] or 'UNKNOWN' in states else
                        'QUERIED_DRIVES_PROTECTED' if all(state == 'PROTECTED' for state in states) else 'NOT_READY')
    return result


def main(argv=None, *, output=None, **dependencies):
    parser = argparse.ArgumentParser(description='Read-only BitLocker status for explicitly scoped local drives. No whole-app coverage claim.')
    parser.add_argument('--home', default=os.environ.get('ALTERA_HOME') or str(CODE_ROOT), help='Instance path; defaults to ALTERA_HOME or the source checkout.')
    parser.add_argument('--additional-path', action='append', default=[], help='Also inspect the resolved drive of this browser/download/cache/service path; repeat as needed.')
    args = parser.parse_args(argv)
    result = inspect_storage(args.home, args.additional_path, **dependencies)
    (output or print)(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result['status'] == 'QUERIED_DRIVES_PROTECTED' else 1 if result['status'] == 'NOT_READY' else 2


if __name__ == '__main__':
    sys.exit(main())
