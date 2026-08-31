"""Software video output bridge for libvlc (Wayland / non-X11 fallback).

Native Wayland has no X11 window handle, so ``set_hwnd`` cannot be used.
Instead we register libvlc's video output callbacks: VLC decodes into a
pre-allocated I420 (YUV420P) buffer we provide, and each completed frame
is converted to ARGB with the *system* libswscale (loaded via ctypes)
before being emitted as a ``QImage``.

VLC is deliberately asked for the I420 display chroma: I420->I420 is a
stable no-op inside VLC, whereas requesting RGB32 makes VLC 3.0.x run its
own (segfaulting) swscale YUV->RGB path. Converting here with libswscale
directly is what ffmpeg itself does, which is why the output is
pixel-identical to an ffmpeg YUV->ARGB conversion.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

# ------------------------------------------------------------------ libswscale
# Loaded via ctypes at import time with graceful failure: if any piece is
# missing the bridge still constructs, and the callbacks become safe
# no-ops (no conversion, no emit, no crash).
_SWS_SONAMES = (
    "libswscale.so.9",
    "libswscale.so.8",
    "libswscale.so.7",
    "libswscale.so.6",
    "libswscale.so.5",
)
_AU_SONAMES = (
    "libavutil.so.60",
    "libavutil.so.59",
    "libavutil.so.58",
    "libavutil.so.57",
    "libavutil.so.56",
)

_SWS_GET_CONTEXT = None
_SWS_SCALE = None
_FMT_YUV420P: int | None = None
_FMT_BGRA: int | None = None
_SWSCALE_AVAILABLE = False


def _find_pix_fmt(au: ctypes.CDLL, name: str) -> int:
    """Resolve a pixel format number by name.

    ffmpeg 8 renumbered the enum ("argb" is 25 there, 6 in older builds),
    so the integers must never be hardcoded.
    """
    f = au.av_pix_fmt_desc_get
    f.restype = ctypes.c_void_p
    f.argtypes = [ctypes.c_int]
    g = au.av_get_pix_fmt_string
    g.restype = ctypes.c_int
    g.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    for i in range(200):
        if not f(i):
            continue
        buf = ctypes.create_string_buffer(64)
        g(buf, 64, i)
        if buf.value.decode(errors="ignore").split()[0] == name:
            return i
    raise RuntimeError(f"{name} not found")


def _init_swscale() -> None:
    """Load libswscale + libavutil and bind the ctypes prototypes."""
    global _SWS_GET_CONTEXT, _SWS_SCALE, _FMT_YUV420P, _FMT_BGRA
    global _SWSCALE_AVAILABLE
    ss = au = None
    for soname in _SWS_SONAMES:
        try:
            ss = ctypes.CDLL(soname)
            break
        except OSError:
            continue
    if ss is None:
        return
    for soname in _AU_SONAMES:
        try:
            au = ctypes.CDLL(soname)
            break
        except OSError:
            continue
    if au is None:
        return
    try:
        _FMT_YUV420P = _find_pix_fmt(au, "yuv420p")
        # Destination must be "bgra": memory layout [B,G,R,A] with A=0xFF
        # (opaque), which is exactly QImage.Format_ARGB32.  The "argb"
        # format has the same color byte order but an UNDEFINED/variable
        # alpha byte (measured 0x00 for ~45% of pixels), so Qt would
        # render those pixels transparent.  Verified: bgra output is
        # pixel-identical to ffmpeg's own YUV->RGB conversion.
        _FMT_BGRA = _find_pix_fmt(au, "bgra")
        _SWS_GET_CONTEXT = ss.sws_getContext
        _SWS_GET_CONTEXT.restype = ctypes.c_void_p  # 64-bit pointer, critical
        _SWS_GET_CONTEXT.argtypes = [ctypes.c_int] * 7 + [ctypes.c_void_p] * 3
        _SWS_SCALE = ss.sws_scale
        _SWS_SCALE.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int),
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int),
        ]
    except (AttributeError, OSError, RuntimeError):
        return
    _SWSCALE_AVAILABLE = True


_init_swscale()

_SWS_BILINEAR = 2

# Exact C prototypes (libvlc 3.x).  chroma is a raw c_void_p on purpose:
# c_char_p would arrive in Python as immutable bytes and in-place writes
# would be silently lost.
_FnLock = ctypes.CFUNCTYPE(
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
)
_FnUnlock = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
)
_FnSetup = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
)
_FnCleanup = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_void_p))


def video_output_mode(platform_name: str) -> str:
    """Pick the video output strategy for a Qt platform name.

    "xcb" (X11 / XWayland) and "windows" can render into a native window
    handle (hardware accelerated). Everything else — notably native
    "wayland" — needs the software callback renderer.
    """
    return "hwnd" if platform_name in ("xcb", "windows") else "software"


class FrameBridge(QObject):
    """Owns the libvlc video output callbacks and forwards frames to the UI.

    The CFUNCTYPE wrappers are stored as attributes: libvlc keeps only the
    raw C pointers, so the Python wrappers must be kept alive here.
    """

    frame_ready = Signal(QImage)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._w = 0
        self._h = 0
        self._yuv = None  # planar I420: Y (w*h) + U (w/2*h/2) + V
        self._rgb = None  # w*4*h ARGB, created lazily on first unlock
        self._ctx = None  # sws scale context, created lazily on first unlock
        self._lock = _FnLock(self._on_lock)
        self._unlock = _FnUnlock(self._on_unlock)
        self._setup = _FnSetup(self._on_setup)
        self._cleanup = _FnCleanup(self._on_cleanup)

    @property
    def swscale_available(self) -> bool:
        """True when the system libswscale could be loaded and bound."""
        return _SWSCALE_AVAILABLE

    @property
    def video_callbacks(self) -> tuple:
        """(lock, unlock) for MediaPlayer.video_set_callbacks.

        Display and opaque are passed by the caller as (None, None).
        """
        return (self._lock, self._unlock)

    @property
    def format_callbacks(self) -> tuple:
        """(setup, cleanup) for MediaPlayer.video_set_format_callbacks."""
        return (self._setup, self._cleanup)

    def reset(self) -> None:
        """Drop the current buffers (e.g. when playback stops)."""
        self._w = 0
        self._h = 0
        self._yuv = None
        self._rgb = None
        self._ctx = None

    # -------------------------------------------------------------- callbacks
    def _on_setup(self, opaque_ptr, chroma, width, height, pitches, lines) -> int:
        """Ask VLC for I420 at the decoder's size; (re)allocate the buffers."""
        try:
            w = width[0]
            h = height[0]
            if w <= 0 or h <= 0:
                return 0
            self._w, self._h = w, h
            self._yuv = (ctypes.c_uint8 * (w * h + 2 * (w // 2) * (h // 2)))()
            self._rgb = None
            self._ctx = None
            pitches[0] = w
            pitches[1] = w // 2
            pitches[2] = w // 2
            lines[0] = h
            lines[1] = h // 2
            lines[2] = h // 2
            ctypes.memmove(chroma, b"I420\0", 5)
            return 1
        except Exception:
            return 0

    def _on_lock(self, opaque, planes) -> int:
        """Hand VLC the three I420 planes to decode into."""
        try:
            if self._yuv is None:
                return 0
            base = ctypes.addressof(self._yuv)
            planes[0] = base
            planes[1] = base + self._w * self._h
            planes[2] = base + self._w * self._h + (self._w // 2) * (self._h // 2)
            return base
        except Exception:
            return 0

    def _on_unlock(self, opaque, picture, planes) -> None:
        """Frame complete: convert YUV420P -> ARGB via libswscale and emit.

        VLC passes the decoder buffer size (e.g. 320x258 for a 320x240
        video); converting the full buffer height is correct and intended.
        """
        try:
            if self._yuv is None or self._w <= 0 or not self.swscale_available:
                return
            w, h = self._w, self._h
            if self._ctx is None or self._rgb is None:
                self._ctx = _SWS_GET_CONTEXT(
                    w, h, _FMT_YUV420P, w, h, _FMT_BGRA, _SWS_BILINEAR,
                    None, None, None,
                )
                self._rgb = (ctypes.c_uint8 * (w * 4 * h))()
                if self._ctx is None:
                    return
            base = ctypes.addressof(self._yuv)
            src_data = (ctypes.c_void_p * 3)()
            src_data[0] = base
            src_data[1] = base + w * h
            src_data[2] = base + w * h + (w // 2) * (h // 2)
            strides = (ctypes.c_int * 3)(w, w // 2, w // 2)
            dst_data = (ctypes.c_void_p * 1)()
            dst_data[0] = ctypes.addressof(self._rgb)
            dst_strides = (ctypes.c_int * 1)(w * 4)
            _SWS_SCALE(self._ctx, src_data, strides, 0, h, dst_data, dst_strides)
            # .copy() is mandatory: VLC reuses this buffer for the next frame.
            img = QImage(
                memoryview(self._rgb), w, h, w * 4, QImage.Format_ARGB32
            ).copy()
            self.frame_ready.emit(img)
        except Exception:
            pass

    def _on_cleanup(self, opaque_ptr) -> None:
        """VLC is done with the output: drop the buffers."""
        try:
            self.reset()
        except Exception:
            pass
