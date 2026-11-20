"""File fingerprints that mean the same thing from a path and from an open file, on every supported Python."""
import os


def ctime_ns(info):
    """st_ctime_ns, except on Windows, where it is the file's creation time.

    From Python 3.12 on Windows, st_ctime from a path (stat, lstat) is the creation time but from an open file (fstat)
    it is the last metadata change, so a fingerprint taken one way never matches one taken the other way. The creation
    time agrees both ways: st_birthtime_ns from 3.12, and st_ctime_ns before that, which was the creation time too.
    Elsewhere st_ctime is the change time, the same both ways.
    """
    if os.name == 'nt':
        return getattr(info, 'st_birthtime_ns', info.st_ctime_ns)
    return info.st_ctime_ns
