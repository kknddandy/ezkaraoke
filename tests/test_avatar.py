"""Tests for ezkaraoke.avatar (provider dispatch, validation, worker).

All network I/O is replaced by a fake ``_http_get`` dispatching on URL
substrings, so nothing in here touches the real network.
"""

import os
import time
import urllib.error

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from ezkaraoke import avatar as avatar_mod

IMAGE_BYTES = b"\xff\xd8\xff\xe0" + b"J" * 200  # JPEG magic + >100 bytes
WIKI_JSON = (
    b'{"query":{"pages":{"1":{"pageid":1,'
    b'"thumbnail":{"source":"http://img/fake.jpg"}}}}}'
)
BAIKE_JSON = '{"key":"周杰伦","image":"http://bkimg/fake.jpg"}'.encode("utf-8")


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def make_fake_http(responses: dict):
    """Dispatch on URL substring; values may be bytes or an exception to raise."""

    def fake(url: str, timeout: int = 10) -> bytes:
        for key, value in responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected URL: {url}")

    return fake


def stub_search_offline(monkeypatch):
    """Keep the Bing fallback from touching the real network."""
    monkeypatch.setattr(
        avatar_mod, "_browser_get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")),
    )


def test_wiki_hit_returns_image_bytes(monkeypatch):
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http({"zh.wikipedia.org": WIKI_JSON, "img/fake.jpg": IMAGE_BYTES}),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") == IMAGE_BYTES


def test_wiki_failure_falls_back_to_baike(monkeypatch):
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http(
            {
                "zh.wikipedia.org": urllib.error.URLError("boom"),
                "baike.baidu.com": BAIKE_JSON,
                "bkimg/fake.jpg": IMAGE_BYTES,
            }
        ),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") == IMAGE_BYTES


def test_all_providers_fail_returns_none(monkeypatch):
    stub_search_offline(monkeypatch)
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http(
            {
                "zh.wikipedia.org": urllib.error.URLError("boom"),
                "baike.baidu.com": urllib.error.URLError("boom"),
            }
        ),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") is None


def test_non_image_payload_rejected(monkeypatch):
    stub_search_offline(monkeypatch)
    payload = b"<html>error</html>" + b" " * 200  # >100 bytes, no image magic
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http({"zh.wikipedia.org": WIKI_JSON, "img/fake.jpg": payload}),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") is None


def test_small_payload_rejected(monkeypatch):
    stub_search_offline(monkeypatch)
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http(
            {"zh.wikipedia.org": WIKI_JSON, "img/fake.jpg": b"\xff\xd8\xff" + b"J" * 50}
        ),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") is None


def test_baike_key_mismatch_rejected(monkeypatch):
    stub_search_offline(monkeypatch)
    mismatched = '{"key":"别人","image":"http://bkimg/fake.jpg"}'.encode("utf-8")
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http(
            {
                "zh.wikipedia.org": urllib.error.URLError("boom"),
                "baike.baidu.com": mismatched,
                "bkimg/fake.jpg": IMAGE_BYTES,
            }
        ),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") is None


def test_search_query_cjk_and_ascii():
    assert avatar_mod._search_query("周传雄") == "歌手 周传雄"
    assert avatar_mod._search_query("Coldplay") == "Coldplay singer"


BING_PAGE = (
    b'<a m="{&quot;turl&quot;:&quot;http://t/1.jpg&quot;,'
    b'&quot;murl&quot;:&quot;http://cdn/cand1.jpg&quot;}"></a>'
    b'<a m="{&quot;turl&quot;:&quot;http://t/2.jpg&quot;,'
    b'&quot;murl&quot;:&quot;http://cdn/cand2.jpg&quot;}"></a>'
)


def _jpeg(width: int, height: int, noisy: bool = False) -> bytes:
    """Solid JPEGs compress below the 5KB minimum, so *noisy* fills random RGB."""
    import io
    import os

    from PIL import Image

    if noisy:
        img = Image.frombuffer("RGB", (width, height), os.urandom(width * height * 3))
    else:
        img = Image.new("RGB", (width, height), (120, 60, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_bing_image_urls_parsing(monkeypatch):
    monkeypatch.setattr(
        avatar_mod, "_browser_get",
        lambda url, referer=None, timeout=10: BING_PAGE,
    )
    assert avatar_mod._bing_image_urls("周传雄") == [
        "http://cdn/cand1.jpg", "http://cdn/cand2.jpg",
    ]


def test_full_chain_falls_back_to_search(monkeypatch):
    """wiki 429 + baike down -> Bing candidate that is a real photo wins."""
    small = _jpeg(100, 100, noisy=True)      # >5KB but below the minimum edge
    good = _jpeg(300, 300, noisy=True)       # real photo-sized
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http(
            {
                "zh.wikipedia.org": urllib.error.HTTPError(
                    "x", 429, "Too Many Requests", None, None
                ),
                "baike.baidu.com": urllib.error.URLError("boom"),
            }
        ),
    )
    seen: list[str] = []

    def fake_browser(url, referer=None, timeout=10):
        seen.append(url)
        if "bing.com" in url:
            return BING_PAGE
        if "cand1" in url:
            return small
        if "cand2" in url:
            return good
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(avatar_mod, "_browser_get", fake_browser)
    assert avatar_mod.fetch_artist_avatar("周传雄") == good
    assert any("cand2" in u for u in seen)


def test_worker_emits_per_name_and_finishes(qapp, monkeypatch):
    monkeypatch.setattr(
        avatar_mod,
        "fetch_artist_avatar",
        lambda name: b"\xff\xd8\xff" + name.encode("utf-8") * 40,
    )
    names = ["甲乐队", "乙乐队"]
    emitted: list[tuple[str, object]] = []
    done = []
    worker = avatar_mod.AvatarWorker(names)
    worker.fetched.connect(lambda name, data: emitted.append((name, data)))
    worker.finished_all.connect(lambda: done.append(True))
    worker.start()
    deadline = time.time() + 10
    while worker.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert worker.wait(5000)
    for _ in range(100):  # flush queued cross-thread signals
        qapp.processEvents()
    assert [name for name, _ in emitted] == names
    assert all(data is not None for _, data in emitted)
    assert done == [True]


def test_worker_progress_signal(qapp, monkeypatch):
    monkeypatch.setattr(
        avatar_mod,
        "fetch_artist_avatar",
        lambda name: b"\xff\xd8\xff" + name.encode("utf-8") * 40,
    )
    names = ["甲乐队", "乙乐队", "丙乐队"]
    progress: list[tuple[int, int]] = []
    worker = avatar_mod.AvatarWorker(names)
    worker.progress.connect(lambda done, total: progress.append((done, total)))
    worker.start()
    deadline = time.time() + 10
    while worker.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert worker.wait(5000)
    for _ in range(100):
        qapp.processEvents()
    assert progress == [(1, 3), (2, 3), (3, 3)]


# ------------------------------------------------------------------ PNG iCCP
def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    import struct
    import zlib

    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _png_with_iccp() -> bytes:
    """Minimal 1x1 RGB PNG carrying a (fake) iCCP chunk and a gAMA chunk."""
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x12\x34\x56")
    iccp = b"sRGB\x00\x00" + zlib.compress(b"fake-icc-profile-payload")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"iCCP", iccp)
        + _png_chunk(b"gAMA", struct.pack(">I", 45455))
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _chunk_types(data: bytes) -> list[bytes]:
    import struct

    types, i = [], 8
    while i + 8 <= len(data):
        length = struct.unpack(">I", data[i : i + 4])[0]
        types.append(data[i + 4 : i + 8])
        i += 12 + length
    return types


def test_strip_png_iccp_removes_only_iccp():
    png = _png_with_iccp()
    stripped = avatar_mod.strip_png_iccp(png)
    assert _chunk_types(stripped) == [b"IHDR", b"gAMA", b"IDAT", b"IEND"]
    assert stripped != png


def test_strip_png_iccp_decodes_to_same_pixels():
    import io

    from PIL import Image

    png = _png_with_iccp()
    before = Image.open(io.BytesIO(png))
    after = Image.open(io.BytesIO(avatar_mod.strip_png_iccp(png)))
    assert before.tobytes() == after.tobytes()


def test_strip_png_iccp_passthrough_without_iccp():
    import struct
    import zlib

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x12\x34\x56")
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    assert avatar_mod.strip_png_iccp(png) == png


def test_strip_png_iccp_non_png_and_truncated_unchanged():
    jpeg = b"\xff\xd8\xff\xe0" + b"junk" * 10
    assert avatar_mod.strip_png_iccp(jpeg) == jpeg
    png = _png_with_iccp()
    assert avatar_mod.strip_png_iccp(png[:20]) == png[:20]


# --- Regression: malformed embedded ICC profile triggers Qt warnings -------
#
# Some web images embed ICC profiles whose tag table contains offsets that
# are not 4-byte aligned. Qt logs "qt.gui.icc: fromIccProfile: invalid tag
# offset alignment" while decoding such PNGs. The profile only tweaks
# colourimetry, so the search path strips the iCCP chunk before decoding.

# First 128 bytes of a real D50 display profile (ECI-RGBv1.icc): header +
# illuminant, which is exactly what Qt's isValidIccProfile() validates.
_ECI_HEADER_B64 = (
    "AABQLGxjbXMEQAAAbW50clJHQiBYWVogB+oAAgAQABEAKQAAYWNzcEFQUEw"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPbWAAEAAAAA0y1sY21z0ASn"
    "PgwsgZuVgFNRevmlygAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


def _trigger_profile() -> bytes:
    """Near-real ICC display profile (D50) with a tag offset not 4-byte aligned."""
    import base64
    import struct

    prof = bytearray(base64.b64decode(_ECI_HEADER_B64))  # real profile header
    total = 152
    prof[0:4] = struct.pack(">I", total)
    prof += struct.pack(">I", 1)  # tagCount
    prof += b"desc" + struct.pack(">II", 141, 8)  # offset 141 == 4n+1
    prof += b"\x00" * (total - len(prof))
    return bytes(prof)


def _trigger_png() -> bytes:
    # Noise pixels keep the file incompressible so it clears the 5 KB
    # minimum-size filter in _search_image_data.
    import io
    import os

    from PIL import Image

    img = Image.frombytes("RGB", (120, 120), os.urandom(120 * 120 * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG", icc_profile=_trigger_profile())
    return buf.getvalue()


def _icc_warnings(data: bytes) -> list[str]:
    msgs: list[str] = []
    qInstallMessageHandler(lambda *a: msgs.append(" ".join(str(x) for x in a)))
    try:
        img = QImage()
        img.loadFromData(data)
    finally:
        qInstallMessageHandler(None)
    return [m for m in msgs if "icc" in m.lower()]


def test_malformed_iccp_png_warns_when_loaded_raw(qapp):
    # Guards the fixture: if Qt stops warning about this profile, the
    # regression test below would prove nothing.
    assert _icc_warnings(_trigger_png())


def test_search_image_data_strips_iccp_and_qt_stays_silent(monkeypatch, qapp):
    trigger = _trigger_png()
    monkeypatch.setattr(
        avatar_mod, "_bing_image_urls", lambda name: ["http://test/cand.png"]
    )
    monkeypatch.setattr(avatar_mod, "_browser_get", lambda *a, **k: trigger)
    result = avatar_mod._search_image_data("Test Artist")
    assert result is not None
    assert b"iCCP" not in _chunk_types(result)
    assert _icc_warnings(result) == []
