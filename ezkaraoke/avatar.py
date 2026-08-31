"""Artist avatar fetching (Wikipedia zh -> Baidu Baike fallback)."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, Signal

USER_AGENT = "Mozilla/5.0 (ezkaraoke home KTV app)"
_WIKI_API = "https://zh.wikipedia.org/w/api.php"
_BAIKE_API = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"


def _http_get(url: str, timeout: int = 10) -> bytes:
    """GET *url* with UA header. Module-level so tests can monkeypatch it."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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


def fetch_artist_avatar(name: str) -> bytes | None:
    """Try providers in order; return image bytes or None. Never raises.

    Require len(data) > 100 and _looks_like_image(data) before accepting.
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
