"""Inspectable retrieval priority, deliberately independent of claim confidence.

Age changes ordering only. No memory is deleted, made true, or made eligible by
this score. Old timestamps without an offset stay neutral until a known anchor.
"""
from datetime import datetime, UTC
import math
import re

HALF_LIFE_DAYS = 30
MIN_DECAY = .2
_COMMON = frozenset('a an the i me my you your we our it its this that these those is are was were be been '
                    'have has had do does did can could would will should what when where how why who '
                    'and or but if to of for from in on at with as about tell said says know remember'.split())


def instant(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def describe(metadata, now=None, similarity=None):
    """Pure display/ranking receipt, with no mutation or model call."""
    metadata = metadata or {}
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError('Retrieval priority requires an aware clock.')
    salience = metadata.get('salience', 3)
    salience = salience if type(salience) is int and 1 <= salience <= 5 else 3
    pinned = metadata.get('pinned') is True
    anchors = [(key, instant(metadata.get(key))) for key in ('last_recalled_at', 'salience_anchor_at', 'created_at')]
    known = [(key, value) for key, value in anchors if value is not None and value <= now]
    basis, anchor = max(known, key=lambda item: item[1]) if known else ('unknown_legacy_time', None)
    age = max(0., (now - anchor).total_seconds() / 86400) if anchor else None
    factor = 1. if pinned or age is None else max(MIN_DECAY, 2 ** (-age / HALF_LIFE_DAYS))
    priority = salience / 5 * factor
    relevance = None
    if isinstance(similarity, (int, float)) and not isinstance(similarity, bool) and math.isfinite(similarity):
        relevance = max(0., min(1., float(similarity)))
    score = relevance * (.5 + .5 * priority) + (.18 if pinned else 0.) if relevance is not None else None
    count, revision = metadata.get('recall_count', 0), metadata.get('priority_revision', 0)
    return {'salience': salience, 'pinned': pinned, 'decay_factor': round(factor, 6),
            'age_days': round(age, 4) if age is not None else None,
            'age_basis': basis, 'half_life_days': HALF_LIFE_DAYS, 'minimum_decay': MIN_DECAY,
            'last_recalled_at': metadata.get('last_recalled_at') if instant(metadata.get('last_recalled_at')) else None,
            'recall_count': count if type(count) is int and count >= 0 else 0,
            'revision': revision if type(revision) is int and revision >= 0 else 0,
            'priority_score': round(priority, 6), 'ranking_score': round(score, 6) if score is not None else None,
            'confidence_used': False, 'deletes_memory': False}


def lexical_relevance(query, content):
    """Conservative fallback for bounded pinned candidates outside vector results."""
    def words(value):
        return {word for word in re.findall(r'[^\W_]{2,}', (value or '').casefold()) if word not in _COMMON}
    wanted = words(query)
    return len(wanted & words(content)) / len(wanted) if wanted else 0.
