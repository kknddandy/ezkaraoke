"""Artist avatar fetching.

Provider chain: Wikipedia zh lead image -> Baidu Baike card image ->
Bing image search (last resort, size-filtered).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QBuffer, QByteArray, QIODevice, Qt, Signal
from PySide6.QtGui import QImage

USER_AGENT = "Mozilla/5.0 (ezkaraoke home KTV app)"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_WIKI_API = "https://zh.wikipedia.org/w/api.php"
_BAIKE_API = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
_BING_IMAGES = "https://www.bing.com/images/search"
_MIN_SEARCH_EDGE = 120  # px, reject logos/strips from search results
_MAX_SEARCH_CANDIDATES = 6
_RENDER_MAX_EDGE = 224  # px, 2x the 112px icon (HiDPI); bigger avatars get re-encoded
_RENDER_BATCH = 16  # avatars handed to the GUI per queued signal


def _http_get(url: str, timeout: int = 10) -> bytes:
    """GET *url* with UA header. Module-level so tests can monkeypatch it."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _browser_get(url: str, referer: str | None = None, timeout: int = 10) -> bytes:
    """GET *url* with a browser-like UA (image search + hotlinked images)."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _looks_like_image(data: bytes) -> bool:
    if data[:3] == b"\xff\xd8\xff":  # JPEG
        return True
    if data[:4] == b"\x89PNG":
        return True
    if data[:4] == b"GIF8":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8"


def strip_jpeg_icc(data: bytes) -> bytes:
    """Remove APP2 (embedded ICC profile) segments from JPEG data.

    Web photos frequently embed ICC profiles; malformed ones make Qt log
    ``qt.gui.icc: fromIccProfile: invalid tag offset alignment`` on every
    decode. The profile only tweaks colourimetry, which matters not at all
    for a 112px avatar — dropping the segments silences the warning.
    Non-JPEG or structurally broken data is returned unchanged.
    """
    if data[:2] != _JPEG_SIGNATURE or len(data) < 6:
        return data
    segments: list[bytes] = [data[:2]]
    i, n = 2, len(data)
    stripped = False
    while i + 4 <= n and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # SOS: everything after is image data
            if stripped:
                segments.append(data[i:])
                return b"".join(segments)
            return data
        if marker == 0x00 or 0xD0 <= marker <= 0xD9:  # fill / RST / SOI / EOI
            if stripped:
                segments.append(data[i : i + 2])
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2 or i + 2 + length > n:
            break  # broken structure: don't risk corrupting a valid image
        if marker == 0xE2:  # APP2: ICC profile segment
            stripped = True
        elif stripped:
            segments.append(data[i : i + 2 + length])
        i += 2 + length
    return data  # no APP2 found, or broken structure


def strip_png_iccp(data: bytes) -> bytes:
    """Remove iCCP chunks and duplicate eXIf chunks from PNG data.

    Qt parses embedded ICC profiles and malformed ones trigger
    ``qt.gui.icc: fromIccProfile: invalid tag offset alignment`` on every
    decode; duplicate eXIf chunks trigger ``libpng warning: eXIf:
    duplicate``. The metadata only tweaks colourimetry/EXIF, which matters
    not at all for a 112px avatar — dropping it silences the warnings.
    Non-PNG or structurally broken data is returned unchanged.
    """
    if data[:8] != _PNG_SIGNATURE:
        return data
    out = bytearray(data[:8])
    i = 8
    stripped = False
    seen_exif = False
    while i + 8 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        ctype = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > len(data):
            return data  # truncated chunk: don't risk corrupting a valid image
        if ctype == b"iCCP":
            stripped = True
        elif ctype == b"eXIf":
            if seen_exif:
                stripped = True  # keep only the first eXIf chunk
            else:
                seen_exif = True
                out += data[i:end]
        else:
            out += data[i:end]
        if ctype == b"IEND":
            break
        i = end
    else:
        return data  # no IEND reached: not a clean PNG
    return bytes(out) if stripped else data


def clean_image_data(data: bytes) -> bytes:
    """Strip decode-warning triggers (embedded ICC, duplicate eXIf)."""
    if data[:8] == _PNG_SIGNATURE:
        return strip_png_iccp(data)
    if data[:3] == b"\xff\xd8\xff":
        return strip_jpeg_icc(data)
    return data


def normalize_avatar(data: bytes, max_edge: int = _RENDER_MAX_EDGE) -> bytes | None:
    """Clean *data* and shrink it to a small clean JPEG when it is large.

    Bing full-size hits can be multi-megapixel photos; decoding them takes
    up to a second each and the BLOBs bloat the database. A 224px JPEG
    decodes in well under a millisecond, and re-encoding drops the embedded
    ICC / eXIf chunks that trigger ``qt.gui.icc`` / ``libpng`` warnings.
    Already-small images are returned cleaned (no re-encode). Returns None
    when the image cannot be decoded at all.
    """
    cleaned = clean_image_data(data)
    img = QImage()
    if not img.loadFromData(cleaned):
        return None
    if max(img.width(), img.height()) <= max_edge:
        return cleaned
    if img.hasAlphaChannel():
        img = img.convertToFormat(QImage.Format.Format_RGB32)
    img = img.scaled(
        max_edge,
        max_edge,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "JPEG", 85):
        buf.close()
        return cleaned
    buf.close()
    return bytes(buf.data()) or cleaned


def _wiki_image_url(name: str) -> str | None:
    params = {
        "action": "query",
        "generator": "search",
        "gsrlimit": "1",
        "prop": "pageimages",
        "piprop": "thumbnail",
        "pithumbsize": "300",
        "format": "json",
        "origin": "*",
        "gsrsearch": name,
    }
    url = _WIKI_API + "?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url).decode("utf-8"))
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        source = page.get("thumbnail", {}).get("source")
        if source:
            return source
    return None


def _baike_image_url(name: str) -> str | None:
    url = _BAIKE_API + "?" + urllib.parse.urlencode(
        {"scope": "103", "format": "json", "appid": "379020", "bk_key": name}
    )
    data = json.loads(_http_get(url).decode("utf-8"))
    # Only trust the card when the resolved lemma is exactly the queried name.
    if data.get("key") != name:
        return None
    return data.get("image") or None


def _search_query(name: str) -> str:
    """Query for the image search: "歌手 周传雄" / "Coldplay singer"."""
    if any("\u4e00" <= c <= "\u9fff" for c in name):
        return f"歌手 {name}"
    return f"{name} singer"


def _bing_image_urls(name: str) -> list[str]:
    """Candidate image URLs from the Bing image search result page.

    Prefers the thumbnail (``turl``, a few hundred px) over the full-size
    original (``murl``, often 5-15 MP): avatars render at 112px, and
    full-size originals used to bloat the avatar BLOBs and make every
    startup decode multi-megapixel photos. Falls back to ``murl`` when a
    result carries no ``turl``.
    """
    url = _BING_IMAGES + "?" + urllib.parse.urlencode(
        {"q": _search_query(name), "first": "1", "count": "20", "form": "HDRSC2"}
    )
    page = _browser_get(url).decode("utf-8", "ignore")
    found: list[str] = []
    for m in re.finditer(r'm="(\{&quot;.*?\})"', page):
        obj = html.unescape(m.group(1))
        candidate = None
        mm = re.search(r'"turl":"(http[^"]+)"', obj)
        if mm:
            candidate = html.unescape(mm.group(1))
        else:
            mm = re.search(r'"murl":"(http[^"]+)"', obj)
            if mm:
                candidate = html.unescape(mm.group(1))
        if candidate and candidate not in found:
            found.append(candidate)
        if len(found) >= _MAX_SEARCH_CANDIDATES:
            break
    return found


def _search_image_data(name: str) -> bytes | None:
    """Last-resort provider: download search candidates until one is a real photo."""
    for url in _bing_image_urls(name):
        try:
            data = _browser_get(url, referer=_BING_IMAGES)
        except Exception:  # noqa: BLE001 - dead candidate -> next
            continue
        if len(data) <= 5000 or not _looks_like_image(data):
            continue
        data = clean_image_data(data)  # malformed ICC/eXIf triggers qt warnings
        img = QImage()
        if img.loadFromData(data) and min(img.width(), img.height()) >= _MIN_SEARCH_EDGE:
            return normalize_avatar(data) or data
    return None


def fetch_artist_avatar(name: str) -> bytes | None:
    """Try providers in order; return image bytes or None. Never raises.

    Require len(data) > 100 and _looks_like_image(data) before accepting
    (search candidates additionally need real photo dimensions).
    """
    for provider in (_wiki_image_url, _baike_image_url):
        try:
            image_url = provider(name)
        except Exception:  # noqa: BLE001 - network/JSON failure -> next provider
            continue
        if not image_url:
            continue
        try:
            data = _http_get(image_url)
        except Exception:  # noqa: BLE001
            continue
        if len(data) > 100 and _looks_like_image(data):
            return normalize_avatar(data) or clean_image_data(data)
    try:
        return _search_image_data(name)
    except Exception:  # noqa: BLE001 - search is best-effort
        return None


class _AvatarSignals(QObject):
    """Qt signals owned by the worker; emitted from the worker thread.

    Created on the GUI thread, so cross-thread emits are queued onto the
    GUI event loop (thread-safe, auto-connection).
    """

    fetched = Signal(str, object)  # (artist, bytes | None)
    progress = Signal(int, int)  # (done, total)
    finished_all = Signal()


class AvatarWorker(threading.Thread):
    """Fetches avatars for *names* on a daemon background thread.

    Emits ``fetched(artist, bytes | None)`` per name, ``progress(done,
    total)`` after each, and ``finished_all`` at the end. No DB access
    here — the window handler persists results.

    The thread is a daemon: a hung network call can never block window
    close or crash process exit (the previous QThread version could stall
    close for minutes or abort finalization). ``stop()`` asks it to finish
    after the current fetch.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(daemon=True)
        self.names = list(names)
        self._stop = threading.Event()
        self._signals = _AvatarSignals()

    @property
    def fetched(self):
        return self._signals.fetched

    @property
    def progress(self):
        return self._signals.progress

    @property
    def finished_all(self):
        return self._signals.finished_all

    def stop(self) -> None:
        """Ask the worker to stop after the current fetch (bounded by its timeout)."""
        self._stop.set()

    def isRunning(self) -> bool:  # noqa: N802 - QThread-compatible name
        return self.is_alive()

    def wait(self, ms: int = 5000) -> bool:  # noqa: A003 - QThread-compatible name
        self.join(ms / 1000.0)
        return not self.is_alive()

    def run(self) -> None:
        total = len(self.names)
        for done, name in enumerate(self.names, 1):
            if self._stop.is_set():
                break
            data = fetch_artist_avatar(name)
            if self._stop.is_set():
                break
            self._signals.fetched.emit(name, data)
            self._signals.progress.emit(done, total)
            time.sleep(0.2)  # be polite
        self._signals.finished_all.emit()


class _RendererSignals(QObject):
    """Qt signals owned by the renderer; emitted from the worker thread."""

    rendered = Signal(list)  # batches of (artist, bytes, migrated)
    progress = Signal(int, int)  # (done, total)
    finished_all = Signal()


class AvatarRenderer(threading.Thread):
    """Decodes cached avatar data off the GUI thread.

    The window used to decode every cached avatar synchronously at startup
    (thousands of multi-megapixel photos -> 30-60 s of frozen window).
    This worker instead hands each cleaned image to the GUI in batches as
    soon as it is ready, so the window opens instantly and photos pop in
    one by one.

    First pass over an un-normalized row decodes it: images larger than
    ``_RENDER_MAX_EDGE`` are re-encoded as small clean JPEGs and the
    smaller copy is persisted by THIS worker, cleaning-only rows are
    persisted as cleaned copies, and every processed row is flagged
    ``normalized = 1``. Later launches skip the decode entirely for
    flagged rows (the fetch path flags avatars it normalized on arrival),
    so steady-state startup is pure DB read + handoff, no decoding.

    Opens its own sqlite connection (read/write) — the main
    ``SongDatabase`` connection is bound to the GUI thread; WAL mode
    allows the two to coexist.
    """

    def __init__(self, names: list[str], db_path: str | Path) -> None:
        super().__init__(daemon=True)
        self.names = list(names)
        self.db_path = str(db_path)
        self._stop = threading.Event()
        self._signals = _RendererSignals()

    @property
    def rendered(self):
        return self._signals.rendered

    @property
    def progress(self):
        return self._signals.progress

    @property
    def finished_all(self):
        return self._signals.finished_all

    def stop(self) -> None:
        """Ask the renderer to stop after the current avatar (bounded by its decode)."""
        self._stop.set()

    def isRunning(self) -> bool:  # noqa: N802 - QThread-compatible name
        return self.is_alive()

    def wait(self, ms: int = 5000) -> bool:  # noqa: A003 - QThread-compatible name
        self.join(ms / 1000.0)
        return not self.is_alive()

    def run(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
        except sqlite3.Error:
            self._signals.finished_all.emit()
            return
        try:
            total = len(self.names)
            batch: list[tuple[str, bytes]] = []
            # Databases created outside SongDatabase (tests) may lack the
            # flag column; fall back to the old always-decode path.
            have_flag = "normalized" in {
                r[1] for r in conn.execute("PRAGMA table_info(artists)")
            }

            def persist(name: str, small: bytes) -> None:
                try:
                    conn.execute(
                        "INSERT INTO artists "
                        "(name, avatar, avatar_tried, avatar_tried_at, normalized) "
                        "VALUES (?, ?, 1, ?, 1) "
                        "ON CONFLICT(name) DO UPDATE SET avatar = excluded.avatar, "
                        "avatar_tried = 1, avatar_tried_at = excluded.avatar_tried_at, "
                        "normalized = 1",
                        (name, small, time.time()),
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass  # icon still updates; the pass retries on next launch

            def mark_normalized(name: str) -> None:
                if not have_flag:
                    return
                try:
                    conn.execute(
                        "UPDATE artists SET normalized = 1 WHERE name = ?", (name,)
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass

            def flush() -> None:
                if batch:
                    self._signals.rendered.emit(list(batch))
                    batch.clear()

            for done, name in enumerate(self.names, 1):
                if self._stop.is_set():
                    break
                if have_flag:
                    row = conn.execute(
                        "SELECT avatar, normalized FROM artists WHERE name = ?",
                        (name,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT avatar, 0 FROM artists WHERE name = ?", (name,)
                    ).fetchone()
                data = row[0] if row else None
                if data:
                    if row[1]:
                        small = data  # already cleaned + shrunk: skip the decode
                    else:
                        small = normalize_avatar(data)
                        if small is None:
                            small = clean_image_data(data)
                        if small != data:
                            persist(name, small)  # shrunk/cleaned, off the GUI thread
                        else:
                            mark_normalized(name)
                    batch.append((name, small))
                    if len(batch) >= _RENDER_BATCH:
                        flush()
                self._signals.progress.emit(done, total)
            flush()
        finally:
            conn.close()
        self._signals.finished_all.emit()
