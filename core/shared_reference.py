"""Bounded, metadata-stripping PNG preparation for an explicit shared image.

The normalizer does not establish identity, consent or age, and does not itself
authorize image generation or persist the input.
"""
import binascii
import struct
import zlib

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_CHUNKS = 1024
_SIGNATURE = b'\x89PNG\r\n\x1a\n'


class ReferenceError(ValueError):
    """Fixed error without image bytes or source file names."""


def _chunk(kind, content):
    return (struct.pack('>I', len(content)) + kind + content
            + struct.pack('>I', binascii.crc32(kind + content) & 0xffffffff))


def normalize_png(raw):
    """Return one bounded RGB/RGBA PNG without ancillary metadata.

    The browser will need to convert other formats to an ordinary PNG before
    submission. No native image decoder, network access or filesystem is used.
    """
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES or not raw.startswith(_SIGNATURE):
            raise ValueError()
        offset, chunks, header, parts = len(_SIGNATURE), 0, None, []
        seen_data = ended_data = ended = False
        while offset < len(raw):
            chunks += 1
            if chunks > MAX_CHUNKS or offset + 12 > len(raw):
                raise ValueError()
            length = struct.unpack_from('>I', raw, offset)[0]
            end = offset + 12 + length
            if end > len(raw):
                raise ValueError()
            kind, content = raw[offset + 4:offset + 8], raw[offset + 8:offset + 8 + length]
            crc = struct.unpack_from('>I', raw, offset + 8 + length)[0]
            if (not all(65 <= char <= 90 or 97 <= char <= 122 for char in kind)
                    or (binascii.crc32(kind + content) & 0xffffffff) != crc):
                raise ValueError()
            if header is None:
                if kind != b'IHDR' or length != 13:
                    raise ValueError()
                width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', content)
                if (not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION
                        or width * height > MAX_PIXELS or depth != 8 or color not in (2, 6)
                        or compression != 0 or filtering != 0 or interlace != 0):
                    raise ValueError()
                header = content
                expected = height * (1 + width * (3 if color == 2 else 4))
            elif kind == b'IDAT':
                if ended_data or not length:
                    raise ValueError()
                seen_data = True
                parts.append(content)
            elif kind == b'IEND':
                if length or not seen_data or end != len(raw):
                    raise ValueError()
                ended = True
                break
            else:
                if kind in (b'IHDR', b'acTL', b'fcTL', b'fdAT') or 65 <= kind[0] <= 90:
                    raise ValueError()
                if seen_data:
                    ended_data = True
            offset = end
        if not ended or not parts:
            raise ValueError()
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(b''.join(parts), expected + 1)
        if (len(decoded) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail):
            raise ValueError()
        stride = expected // height
        if any(decoded[index] > 4 for index in range(0, expected, stride)):
            raise ValueError()
        result = _SIGNATURE + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(decoded)) + _chunk(b'IEND', b'')
        if len(result) > MAX_BYTES:
            raise ValueError()
        return result
    except (ValueError, TypeError, struct.error, zlib.error, OverflowError):
        raise ReferenceError('Use a single non-animated PNG image within the size limits.') from None
