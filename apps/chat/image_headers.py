"""Read an image's format and dimensions from its header, without decoding it.

Leading magic bytes alone are not enough: a file that merely starts like a JPEG
reaches the model as an image block the API cannot decode, and the 400 repeats on
every turn that replays it. Each format's header must be well formed and name
positive dimensions.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageHeader:
    media_type: str
    width: int
    height: int


class MalformedImage(ValueError):
    """The bytes start like an image format but its header is not well formed."""


def read_image_header(content: bytes) -> ImageHeader | None:
    """The image's type and size; None when no supported signature matches.

    Raises ``MalformedImage`` when a signature matches but the header does not hold.
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(content)
    if content.startswith(b"\xff\xd8\xff"):
        return _jpeg(content)
    if content.startswith((b"GIF87a", b"GIF89a")):
        return _gif(content)
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return _webp(content)
    return None


def _checked(media_type: str, width: int, height: int) -> ImageHeader:
    if width <= 0 or height <= 0:
        raise MalformedImage(f"{media_type} header has no dimensions")
    return ImageHeader(media_type, width, height)


def _png(content: bytes) -> ImageHeader:
    # The first chunk must be IHDR: length 13, then width and height (big-endian).
    if len(content) < 33 or content[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise MalformedImage("PNG does not start with an IHDR chunk")
    width, height = struct.unpack(">II", content[16:24])
    return _checked("image/png", width, height)


def _gif(content: bytes) -> ImageHeader:
    # Logical screen descriptor: width and height, little-endian, after the signature.
    if len(content) < 13:
        raise MalformedImage("GIF is shorter than its header")
    width, height = struct.unpack("<HH", content[6:10])
    return _checked("image/gif", width, height)


def _webp(content: bytes) -> ImageHeader:
    if len(content) < 30:
        raise MalformedImage("WebP is shorter than its header")
    chunk = content[12:16]
    if chunk == b"VP8 ":
        # Lossy: a keyframe start code, then 14-bit width and height.
        if content[23:26] != b"\x9d\x01\x2a":
            raise MalformedImage("WebP VP8 frame has no start code")
        width, height = struct.unpack("<HH", content[26:30])
        return _checked("image/webp", width & 0x3FFF, height & 0x3FFF)
    if chunk == b"VP8L":
        # Lossless: signature byte, then width-1 and height-1 as 14-bit fields.
        if content[20] != 0x2F:
            raise MalformedImage("WebP VP8L has no signature")
        bits = int.from_bytes(content[21:25], "little")
        return _checked("image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":
        # Extended: canvas width-1 and height-1 as 24-bit fields.
        width = int.from_bytes(content[24:27], "little") + 1
        height = int.from_bytes(content[27:30], "little") + 1
        return _checked("image/webp", width, height)
    raise MalformedImage("WebP has no VP8, VP8L or VP8X chunk")


# Start-of-frame markers carry the dimensions; C4 (DHT), C8 (JPG) and CC (DAC) do not.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers with no length field.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})


def _jpeg(content: bytes) -> ImageHeader:
    position = 2
    while position + 4 <= len(content):
        if content[position] != 0xFF:
            # Like libjpeg's next_marker: skip extraneous bytes some camera firmware
            # writes between segments, up to the next marker.
            position = content.find(b"\xff", position)
            if position == -1:
                break
            continue
        marker = content[position + 1]
        if marker == 0xFF:  # fill byte before a marker
            position += 1
            continue
        if marker == 0x00:  # a stuffed 0xFF is data, not a marker
            position += 2
            continue
        if marker in _JPEG_STANDALONE:
            position += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image, or the scan, before any frame
            break
        (length,) = struct.unpack(">H", content[position + 2 : position + 4])
        if length < 2:
            raise MalformedImage("JPEG segment has an impossible length")
        if marker in _JPEG_SOF:
            if position + 9 > len(content):
                break
            height, width = struct.unpack(">HH", content[position + 5 : position + 9])
            return _checked("image/jpeg", width, height)
        position += 2 + length
    raise MalformedImage("JPEG has no frame header")
