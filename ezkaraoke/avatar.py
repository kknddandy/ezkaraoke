"""Artist avatar fetching.

Provider chain: Wikipedia zh lead image -> Baidu Baike card image ->
Bing image search (last resort, size-filtered).
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, Signal
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


def strip_png_iccp(data: bytes) -> bytes:
    """Remove iCCP chunks from PNG data before decoding.

    Qt parses embedded ICC profiles and malformed ones trigger
    ``qt.gui.icc: fromIccProfile: invalid tag offset alignment`` on every
    decode. The profile only tweaks colourimetry, which matters not at all
    for a 112px avatar — dropping the chunk silences the warning. Non-PNG
    or structurally broken data is returned unchanged.
    """
    if data[:8] != _PNG_SIGNATURE:
        return data
    out = bytearray(data[:8])
    i = 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        ctype = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > len(data):
            return data  # truncated chunk: don't risk corrupting a valid image
        if ctype != b"iCCP":
            out += data[i:end]
        if ctype == b"IEND":
            break
        i = end
    else:
        return data  # no IEND reached: not a clean PNG
    return bytes(out)


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
    """Candidate image URLs from the Bing image search result page."""
    url = _BING_IMAGES + "?" + urllib.parse.urlencode(
        {"q": _search_query(name), "first": "1", "count": "20", "form": "HDRSC2"}
    )
    page = _browser_get(url).decode("utf-8", "ignore")
    found: list[str] = []
    for m in re.finditer(r'm="(\{&quot;.*?\})"', page):
        obj = html.unescape(m.group(1))
        mm = re.search(r'"murl":"(http[^"]+)"', obj)
        if mm:
            candidate = html.unescape(mm.group(1))
            if candidate not in found:
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
        if data[:4] == b"\x89PNG":
            data = strip_png_iccp(data)  # malformed iCCP triggers qt.gui.icc warnings
        img = QImage()
        if img.loadFromData(data) and min(img.width(), img.height()) >= _MIN_SEARCH_EDGE:
            return data
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
            return data
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
