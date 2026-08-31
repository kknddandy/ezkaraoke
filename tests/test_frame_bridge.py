"""Tests for the software video output bridge (libvlc callbacks -> QImage).

All callback logic is exercised with fake buffers; no libvlc, no display.
"""

import ctypes
import sys

import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from ezkaraoke import frame_bridge as fb
from ezkaraoke.frame_bridge import FrameBridge, video_output_mode
from ezkaraoke.player import PlayerController


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


requires_swscale = pytest.mark.skipif(
    not fb._SWSCALE_AVAILABLE, reason="system libswscale not available"
)


# --------------------------------------------------------------------- helpers
def _call_setup(bridge: FrameBridge, w: int = 640, h: int = 360):
    """Invoke the setup callback the way libvlc would."""
    chroma = (ctypes.c_char * 8)()
    ctypes.memmove(chroma, b"I420", 4)
    width = ctypes.c_uint32(w)
    height = ctypes.c_uint32(h)
    pitches = (ctypes.c_uint32 * 3)()
    lines = (ctypes.c_uint32 * 3)()
    ret = bridge._setup(
        None, ctypes.cast(chroma, ctypes.c_void_p), width, height, pitches, lines
    )
    return ret, chroma, pitches, lines


# ------------------------------------------------------------------------ tests
def test_video_output_mode():
    assert video_output_mode("xcb") == "hwnd"
    assert video_output_mode("windows") == "hwnd"
    assert video_output_mode("wayland") == "software"
    assert video_output_mode("offscreen") == "software"


def test_setup_requests_i420_and_allocates():
    bridge = FrameBridge()
    w, h = 640, 360
    ret, chroma, pitches, lines = _call_setup(bridge, w, h)
    assert ret == 1
    assert bytes(chroma[:5]) == b"I420\0"
    assert (pitches[0], pitches[1], pitches[2]) == (w, w // 2, w // 2)
    assert (lines[0], lines[1], lines[2]) == (h, h // 2, h // 2)
    assert (bridge._w, bridge._h) == (w, h)
    assert bridge._yuv is not None
    assert len(bridge._yuv) == w * h + 2 * (w // 2) * (h // 2)
    assert bridge._rgb is None and bridge._ctx is None


def test_setup_rejects_bad_size():
    bridge = FrameBridge()
    ret, _, _, _ = _call_setup(bridge, 0, 0)
    assert ret == 0
    assert bridge._yuv is None


def test_lock_sets_three_planes():
    bridge = FrameBridge()
    _call_setup(bridge)
    w, h = bridge._w, bridge._h
    planes = (ctypes.c_void_p * 3)()
    ret = bridge._lock(None, planes)
    assert ret != 0
    assert ret == planes[0]
    assert planes[1] == ret + w * h
    assert planes[2] == ret + w * h + (w // 2) * (h // 2)


def test_lock_refuses_without_setup():
    bridge = FrameBridge()
    assert not bridge._lock(None, (ctypes.c_void_p * 3)())
    assert not bridge._lock(None, None)  # must not raise


@requires_swscale
def test_unlock_converts_yuv_to_qimage(qapp):
    bridge = FrameBridge()
    w, h = 640, 360
    _call_setup(bridge, w, h)
    # Flat gray: Y=U=V=128 (limited range -> ~126 gray in ARGB).
    gray = bytes([128]) * len(bridge._yuv)
    ctypes.memmove(ctypes.addressof(bridge._yuv), gray, len(gray))

    frames: list[QImage] = []
    bridge.frame_ready.connect(frames.append)
    bridge._unlock(None, None, (ctypes.c_void_p * 3)())
    qapp.processEvents()

    assert len(frames) == 1
    img = frames[0]
    assert (img.width(), img.height()) == (w, h)
    assert img.format() == QImage.Format_ARGB32
    p = img.pixel(0, 0)  # 0xAARRGGBB
    r, g, b = (p >> 16) & 0xFF, (p >> 8) & 0xFF, p & 0xFF
    for c in (r, g, b):
        assert 120 <= c <= 133


def test_unlock_is_safe_without_setup(qapp):
    bridge = FrameBridge()
    frames: list[QImage] = []
    bridge.frame_ready.connect(frames.append)
    bridge._unlock(None, None, (ctypes.c_void_p * 3)())  # must not raise
    bridge._unlock(None, None, None)  # must not raise
    qapp.processEvents()
    assert frames == []


def test_unlock_is_safe_without_swscale(qapp, monkeypatch):
    if not fb._SWSCALE_AVAILABLE:
        pytest.skip("system libswscale not available")
    bridge = FrameBridge()
    _call_setup(bridge)
    monkeypatch.setattr(fb, "_SWSCALE_AVAILABLE", False)
    frames: list[QImage] = []
    bridge.frame_ready.connect(frames.append)
    bridge._unlock(None, None, (ctypes.c_void_p * 3)())  # must not raise
    qapp.processEvents()
    assert frames == []


def test_callback_wrappers_stable():
    bridge = FrameBridge()
    v1, v2 = bridge.video_callbacks, bridge.video_callbacks
    f1, f2 = bridge.format_callbacks, bridge.format_callbacks
    assert len(v1) == 2 and len(f1) == 2
    assert v1[0] is v2[0] and v1[1] is v2[1]
    assert f1[0] is f2[0] and f1[1] is f2[1]


def test_reset_drops_buffers():
    bridge = FrameBridge()
    _call_setup(bridge)
    bridge.reset()
    assert bridge._w == 0 and bridge._yuv is None
    assert not bridge._lock(None, (ctypes.c_void_p * 3)())


def _libvlc_available() -> bool:
    try:
        import vlc

        vlc.Instance()
        return True
    except Exception:
        return False


def test_attach_and_detach_video_callbacks(qapp):
    controller = PlayerController()
    bridge = FrameBridge()
    ok = controller.attach_video_callbacks(
        *bridge.video_callbacks, *bridge.format_callbacks
    )
    assert ok is _libvlc_available()
    controller.detach_video_callbacks()  # must not raise either way
