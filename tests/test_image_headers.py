"""Image header parsing for chat attachments (#913)."""

import pytest

from apps.chat.image_headers import MalformedImage, read_image_header
from tests import image_fixtures as images


@pytest.mark.parametrize(
    ("build", "media_type"),
    [
        (images.png, "image/png"),
        (images.jpeg, "image/jpeg"),
        (images.gif, "image/gif"),
        (images.webp_vp8x, "image/webp"),
        (images.webp_vp8, "image/webp"),
        (images.webp_vp8l, "image/webp"),
    ],
    ids=["png", "jpeg", "gif", "webp-vp8x", "webp-vp8", "webp-vp8l"],
)
def test_reads_type_and_dimensions(build, media_type):
    header = read_image_header(build(1568, 882))
    assert (header.media_type, header.width, header.height) == (media_type, 1568, 882)


@pytest.mark.parametrize(
    "content",
    [
        b"\xff\xd8\xff<html>garbage</html>",
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xda",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        b"GIF89a\x00\x00\x00\x00\x00\x00\x00",
        b"GIF89a",
        b"RIFF\x10\x00\x00\x00WEBPJUNK" + b"\x00" * 20,
        b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 20,
    ],
    ids=[
        "jpeg-garbage",
        "jpeg-scan-before-frame",
        "png-no-ihdr",
        "gif-zero-size",
        "gif-truncated",
        "webp-unknown-chunk",
        "webp-vp8-no-start-code",
    ],
)
def test_malformed_headers_are_refused(content):
    with pytest.raises(MalformedImage):
        read_image_header(content)


def test_jpeg_skips_fill_bytes_and_other_segments():
    content = images.jpeg(10, 20)
    content = content[:2] + b"\xff\xff" + content[2:]
    header = read_image_header(content)
    assert (header.width, header.height) == (10, 20)


def test_non_images_are_not_claimed():
    assert read_image_header(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert read_image_header(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert read_image_header(b'{"a": 1}') is None


def test_jpeg_skips_extraneous_bytes_between_segments():
    content = images.jpeg(10, 20)
    # Two stray zero bytes after APP0, as some camera firmware writes; libjpeg skips them.
    app0_end = 2 + 2 + 16
    content = content[:app0_end] + b"\x00\x00" + content[app0_end:]
    header = read_image_header(content)
    assert (header.width, header.height) == (10, 20)
