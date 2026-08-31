"""Tests for ezkaraoke.avatar (provider dispatch, validation, worker).

All network I/O is replaced by a fake ``_http_get`` dispatching on URL
substrings, so nothing in here touches the real network.
"""

import os
import time
import urllib.error

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
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
    payload = b"<html>error</html>" + b" " * 200  # >100 bytes, no image magic
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http({"zh.wikipedia.org": WIKI_JSON, "img/fake.jpg": payload}),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") is None


def test_small_payload_rejected(monkeypatch):
    monkeypatch.setattr(
        avatar_mod,
        "_http_get",
        make_fake_http(
            {"zh.wikipedia.org": WIKI_JSON, "img/fake.jpg": b"\xff\xd8\xff" + b"J" * 50}
        ),
    )
    assert avatar_mod.fetch_artist_avatar("周杰伦") is None


def test_baike_key_mismatch_rejected(monkeypatch):
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
