"""Retain reply iterators until their explicit cleanup has completed."""
from contextlib import contextmanager






def close_owned(*owners):
    first = None
    seen = set()
    for owner in owners:
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        try:
            close = getattr(owner, 'close', None)
            if close is not None:
                close()
        except BaseException as error:
            if first is None:
                first = error
    if first is not None:
        raise first


@contextmanager
def owned_iterator(source):
    """Close the actual iterator and, when distinct, its source on every exit."""
    try:
        iterator = iter(source)
    except BaseException:
        close_owned(source)
        raise
    try:
        yield iterator
    finally:
        close_owned(iterator, source)
