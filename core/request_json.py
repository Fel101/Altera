"""Bounded, unambiguous JSON for explicit companion controls."""
import json

from flask import request


def companion_body(error, label, allowed, *, avatar=True):
    limit = 16384
    if request.content_length is not None and request.content_length > limit:
        raise error(f'{label} request exceeds 16 KiB.', 413)
    raw = request.get_data(cache=True)
    if len(raw) > limit:
        raise error(f'{label} request exceeds 16 KiB.', 413)

    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError('Duplicate JSON field.')
            values[key] = value
        return values

    def nonfinite(value):
        raise ValueError('Nonfinite JSON number.')

    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite) if request.is_json else None
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        data = None
    scope = {'session_id', 'dataset_epoch'} | ({'avatar_id'} if avatar else set())
    if not isinstance(data, dict) or not set(data) <= set(allowed) | scope:
        raise error(f'Expected a JSON object with supported {label.lower()} fields.', 400)
    if avatar and (not isinstance(data.get('avatar_id'), str) or len(data['avatar_id']) > 128):
        raise error('The frozen avatar_id is required, including an empty string for the default companion.', 400)
    return data
