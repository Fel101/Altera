"""Bounded direct HTTP for reviewed shared-picture providers."""
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 16 * 1024 * 1024








def validate_url(url):
    if (not isinstance(url, str) or not url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in ('\\', '?', '#'))):
        raise ValueError('Invalid configured model-provider URL.')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.port == 0):
            raise ValueError()
    except ValueError:
        raise ValueError('Invalid configured model-provider URL.') from None


class _BoundedStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        count = 0
        for chunk in self.stream:
            count += len(chunk)
            if count > MAX_RESPONSE_BYTES:
                raise httpx.DecodingError('Model-provider response exceeds its size limit.')
            yield chunk

    def close(self):
        self.stream.close()


def _bound(response):
    # Apply the body limit before validating the response headers.
    response.stream = _BoundedStream(response.stream)
    try:
        encodings = response.headers.get_list('content-encoding')
        lengths = response.headers.get_list('content-length')
        if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != 'identity'):
            raise ValueError()
        if lengths:
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise ValueError()
            if int(lengths[0]) > MAX_RESPONSE_BYTES:
                raise ValueError()
    except (ValueError, OverflowError):
        response.close()
        raise httpx.DecodingError('Model-provider response has invalid encoding or size.') from None


def create_transport(base_url):
    validate_url(base_url)
    return httpx.Client(trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(600, connect=5),
                        limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
                        headers={'Accept-Encoding': 'identity'}, event_hooks={'response': [_bound]})
