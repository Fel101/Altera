"""Synthetic image normalization: no upload, model, filesystem or real photos."""
import struct
import zlib

import pytest

from core import shared_reference as reference


def png(width=2, height=2, *, pixels=None, metadata=b'', color=2, depth=8, interlace=0):
    header = struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace)
    pixels = pixels if pixels is not None else (b'\x00' + b'\x01\x02\x03' * width) * height
    extra = reference._chunk(b'tEXt', metadata) if metadata else b''
    return (reference._SIGNATURE + reference._chunk(b'IHDR', header) + extra
            + reference._chunk(b'IDAT', zlib.compress(pixels)) + reference._chunk(b'IEND', b''))


def test_normalization_removes_metadata_and_preserves_single_pixel_stream():
    source = png(metadata=b'private metadata\x00location or identifier')
    result = reference.normalize_png(source)
    assert b'private metadata' not in result and b'tEXt' not in result
    assert result.startswith(reference._SIGNATURE) and reference.normalize_png(result) == result


@pytest.mark.parametrize('source', [b'', b'not a PNG', png() + b'trailing', png(width=4097),
    png(depth=16), png(interlace=1), png(pixels=b'\x05' + b'\x00' * 11),
    png(pixels=b'\x00' + b'\x00' * 500)],
    ids=['empty', 'wrong-format', 'trailing', 'too-wide', 'depth', 'interlaced', 'filter', 'expanded'])
def test_invalid_images_are_rejected_with_a_fixed_error(source):
    with pytest.raises(reference.ReferenceError, match='single non-animated PNG') as caught:
        reference.normalize_png(source)
    assert repr(source[:20]) not in str(caught.value)


def test_bad_crc_and_animated_png_are_rejected():
    original = png()
    bad = bytearray(original)
    bad[29] ^= 1
    animated = original[:33] + reference._chunk(b'acTL', struct.pack('>II', 2, 0)) + original[33:]
    for source in (bytes(bad), animated):
        with pytest.raises(reference.ReferenceError):
            reference.normalize_png(source)


def test_multiple_data_chunks_are_joined_and_extra_compressed_stream_is_refused():
    original = png()
    header_end = 33
    size = struct.unpack_from('>I', original, header_end)[0]
    payload = original[header_end + 8:header_end + 8 + size]
    tail = original[header_end + 12 + size:]
    split = original[:header_end] + reference._chunk(b'IDAT', payload[:2]) + reference._chunk(b'IDAT', payload[2:]) + tail
    assert reference.normalize_png(split) == original
    appended = original[:header_end] + reference._chunk(b'IDAT', payload + zlib.compress(b'other')) + tail
    with pytest.raises(reference.ReferenceError):
        reference.normalize_png(appended)


def test_rejects_oversized_and_excessive_chunk_count(monkeypatch):
    monkeypatch.setattr(reference, 'MAX_BYTES', len(png()) - 1)
    with pytest.raises(reference.ReferenceError):
        reference.normalize_png(png())
    monkeypatch.setattr(reference, 'MAX_BYTES', 1024 * 1024)
    monkeypatch.setattr(reference, 'MAX_CHUNKS', 3)
    with pytest.raises(reference.ReferenceError):
        reference.normalize_png(png(metadata=b'bounded'))
