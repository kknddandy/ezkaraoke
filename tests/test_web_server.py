"""End-to-end tests for the phone-ordering web server (offscreen, port 0).

The server dispatches all db/player work to the GUI thread through a
queued Qt signal and blocks its HTTP worker thread on a threading.Event
until the GUI thread has answered. Each request below is therefore issued
from a worker thread while the main (GUI) thread keeps pumping the event
loop until the response arrives.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from ezkaraoke.database import SongDatabase
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController
from ezkaraoke.web_server import WebServer, lan_ip, qr_pixmap

SONGS = [
    Song("周杰伦", "晴天", "/music/周杰伦-晴天.mp4"),
    Song("周杰伦", "七里香", "/music/周杰伦-七里香.mp4"),
    Song("邓紫棋", "光年之外", "/music/邓紫棋-光年之外.mp4"),
    Song("林俊杰", "江南", "/music/林俊杰-江南.mp4"),
]

QINGTIAN = "/music/周杰伦-晴天.mp4"
QILIXIANG = "/music/周杰伦-七里香.mp4"
GUANGNIAN = "/music/邓紫棋-光年之外.mp4"
JIANGNAN = "/music/林俊杰-江南.mp4"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def request(qapp, base: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    """Perform one HTTP request while pumping the GUI event loop.

    Returns (status, body). Raises AssertionError on transport failure
    (so a deadlock surfaces as a failure, not a hang).
    """
    box: dict = {}

    def worker() -> None:
        try:
            req = urllib.request.Request(base + path, data=body)
            if body is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    box["status"] = resp.status
                    box["body"] = resp.read()
            except urllib.error.HTTPError as e:
                box["status"] = e.code
                box["body"] = e.read()
        except Exception as exc:  # noqa: BLE001 - surfaced as a test failure
            box["error"] = repr(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while thread.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    thread.join(timeout=2)
    assert not thread.is_alive(), "request deadlocked"
    if "error" in box:
        raise AssertionError(f"request failed: {box['error']}")
    return box["status"], box["body"]


def get_json(qapp, base: str, path: str) -> tuple[int, dict]:
    status, body = request(qapp, base, path)
    return status, json.loads(body.decode("utf-8"))


def post_action(qapp, base: str, action: str, **fields) -> tuple[int, dict]:
    data = json.dumps({"action": action, **fields}).encode("utf-8")
    return get_json_raw(qapp, base, "/api/action", data)


def get_json_raw(qapp, base: str, path: str, data: bytes | None) -> tuple[int, dict]:
    status, body = request(qapp, base, path, body=data)
    return status, json.loads(body.decode("utf-8"))


@pytest.fixture
def server(qapp, tmp_path):
    db = SongDatabase(tmp_path / "songs.db")
    db.rebuild(SONGS)
    controller = PlayerController()
    # Never touch libvlc: keeps tests hermetic and immune to EndReached
    # auto-advance while the event loop is pumped during requests.
    controller._start_vlc = lambda: None
    web = WebServer(db, controller, port=0)
    assert web.start()
    yield web
    web.stop()
    qapp.processEvents()
    db.close()


def base_of(server) -> str:
    return f"http://127.0.0.1:{server.url.rsplit(':', 1)[1]}"


# --------------------------------------------------------------------- basics
def test_url_uses_bound_port(server):
    port = int(server.url.rsplit(":", 1)[1])
    assert 0 < port < 65536


def test_serves_phone_ui(server, qapp):
    status, body = request(qapp, base_of(server), "/")
    text = body.decode("utf-8")
    assert status == 200
    assert "手机点歌" in text
    assert "/api/state" in text
    assert "viewport" in text


def test_state_empty(server, qapp):
    status, body = get_json(qapp, base_of(server), "/api/state")
    assert status == 200
    assert body == {
        "state": "stopped",
        "current_index": -1,
        "current": None,
        "queue": [],
        "track": {"multi": False, "index": 0},
    }
    assert body["track"]["multi"] is False


def test_search_by_artist(server, qapp):
    status, body = get_json(
        qapp, base_of(server), "/api/search?q=" + urllib.parse.quote("周杰伦")
    )
    assert status == 200
    # db.search orders by artist, title (Unicode): 七里香 before 晴天
    assert [s["path"] for s in body["results"]] == [QILIXIANG, QINGTIAN]


def test_search_empty_returns_all(server, qapp):
    status, body = get_json(qapp, base_of(server), "/api/search?q=")
    assert status == 200
    assert len(body["results"]) == 4


# ------------------------------------------------------------------------ add
def test_add_auto_plays_when_empty(server, qapp):
    base = base_of(server)
    status, body = post_action(qapp, base, "add", path=QINGTIAN)
    assert status == 200
    assert body["state"] == "playing"
    assert body["current_index"] == 0
    assert body["current"]["path"] == QINGTIAN
    assert [s["path"] for s in body["queue"]] == [QINGTIAN]


def test_add_second_keeps_current_playing(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    status, body = post_action(qapp, base, "add", path=JIANGNAN)
    assert status == 200
    assert body["state"] == "playing"
    assert body["current_index"] == 0
    assert body["current"]["path"] == QINGTIAN
    assert [s["path"] for s in body["queue"]] == [QINGTIAN, JIANGNAN]


def test_add_duplicate_returns_existing_state(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    status, body = post_action(qapp, base, "add", path=QINGTIAN)
    assert status == 200
    assert len(body["queue"]) == 1


def test_add_unknown_path_is_400(server, qapp):
    status, body = post_action(qapp, base_of(server), "add", path="/music/不存在.mp4")
    assert status == 400
    assert "error" in body


# ------------------------------------------------------------------- play_now
def test_play_now_jumps_to_front(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    post_action(qapp, base, "add", path=QILIXIANG)
    status, body = post_action(qapp, base, "play_now", path=GUANGNIAN)
    assert status == 200
    assert body["state"] == "playing"
    assert body["current_index"] == 0
    assert body["current"]["path"] == GUANGNIAN
    assert [s["path"] for s in body["queue"]] == [GUANGNIAN, QINGTIAN, QILIXIANG]


# --------------------------------------------------------------------- remove
def test_remove_current_advances_to_next(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    post_action(qapp, base, "add", path=QILIXIANG)
    status, body = post_action(qapp, base, "remove", index=0)
    assert status == 200
    assert body["state"] == "playing"
    assert body["current_index"] == 0
    assert body["current"]["path"] == QILIXIANG
    assert [s["path"] for s in body["queue"]] == [QILIXIANG]


def test_remove_only_queued_keeps_current(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    post_action(qapp, base, "add", path=QILIXIANG)
    status, body = post_action(qapp, base, "remove", index=1)
    assert status == 200
    assert body["current_index"] == 0
    assert [s["path"] for s in body["queue"]] == [QINGTIAN]


def test_remove_out_of_range_is_noop(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    status, body = post_action(qapp, base, "remove", index=99)
    assert status == 200
    assert len(body["queue"]) == 1


# ----------------------------------------------------------------------- move
def test_move_up(server, qapp):
    base = base_of(server)
    for p in (QINGTIAN, QILIXIANG, JIANGNAN):
        post_action(qapp, base, "add", path=p)
    status, body = post_action(qapp, base, "move", index=2, delta=-1)
    assert status == 200
    assert [s["path"] for s in body["queue"]] == [QINGTIAN, JIANGNAN, QILIXIANG]
    assert body["current_index"] == 0
    assert body["current"]["path"] == QINGTIAN


def test_move_first_up_is_noop(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    post_action(qapp, base, "add", path=QILIXIANG)
    status, body = post_action(qapp, base, "move", index=0, delta=-1)
    assert status == 200
    assert [s["path"] for s in body["queue"]] == [QINGTIAN, QILIXIANG]


# ---------------------------------------------------------------------- clear
def test_clear_stops_and_empties(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    post_action(qapp, base, "add", path=QILIXIANG)
    status, body = post_action(qapp, base, "clear")
    assert status == 200
    assert body["state"] == "stopped"
    assert body["current_index"] == -1
    assert body["current"] is None
    assert body["queue"] == []


# ------------------------------------------------------------------ transport
def test_transport_next_prev_toggle_stop(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    post_action(qapp, base, "add", path=QILIXIANG)

    status, body = post_action(qapp, base, "transport", name="next")
    assert status == 200
    assert body["current_index"] == 1
    assert body["state"] == "playing"

    status, body = post_action(qapp, base, "transport", name="prev")
    assert status == 200
    assert body["current_index"] == 0
    assert body["state"] == "playing"

    status, body = post_action(qapp, base, "transport", name="toggle")
    assert status == 200
    assert body["state"] == "paused"

    status, body = post_action(qapp, base, "transport", name="toggle")
    assert status == 200
    assert body["state"] == "playing"

    status, body = post_action(qapp, base, "transport", name="stop")
    assert status == 200
    assert body["state"] == "stopped"
    assert body["current_index"] == -1
    assert len(body["queue"]) == 2  # the queue is kept


def test_transport_unknown_name_is_400(server, qapp):
    status, body = post_action(qapp, base_of(server), "transport", name="jump")
    assert status == 400
    assert "error" in body


def test_unknown_action_is_400(server, qapp):
    status, body = post_action(qapp, base_of(server), "explode")
    assert status == 400
    assert "error" in body


# ------------------------------------------------------------------ favorite
def test_favorite_toggles(server, qapp):
    base = base_of(server)
    status, body = post_action(qapp, base, "favorite", path=QINGTIAN)
    assert status == 200
    assert "error" not in body
    assert server._db.is_favorite(QINGTIAN) is True
    status, body = post_action(qapp, base, "favorite", path=QINGTIAN)
    assert status == 200
    assert server._db.is_favorite(QINGTIAN) is False


def test_favorite_unknown_path_is_400(server, qapp):
    status, body = post_action(qapp, base_of(server), "favorite", path="/music/不存在.mp4")
    assert status == 400
    assert "error" in body


def test_state_reflects_favorite_on_queue_and_current(server, qapp):
    base = base_of(server)
    post_action(qapp, base, "add", path=QINGTIAN)
    status, body = post_action(qapp, base, "favorite", path=QINGTIAN)
    assert status == 200
    assert body["queue"][0]["favorite"] is True
    status, st = get_json(qapp, base, "/api/state")
    assert status == 200
    assert st["queue"][0]["favorite"] is True
    assert st["current"]["favorite"] is True


def test_search_results_carry_favorite(server, qapp):
    base = base_of(server)
    q = "/api/search?q=" + urllib.parse.quote("晴天")
    status, body = get_json(qapp, base, q)
    assert status == 200
    assert [s["path"] for s in body["results"]] == [QINGTIAN]
    assert all(isinstance(s["favorite"], bool) for s in body["results"])
    assert body["results"][0]["favorite"] is False
    post_action(qapp, base, "favorite", path=QINGTIAN)
    status, body = get_json(qapp, base, q)
    assert status == 200
    assert body["results"][0]["favorite"] is True


# --------------------------------------------------------------------- track
def test_track_default_state_is_single(server, qapp):
    status, body = get_json(qapp, base_of(server), "/api/state")
    assert status == 200
    assert body["track"]["multi"] is False
    assert body["track"]["index"] == 0


def test_track_unknown_name_is_400(server, qapp):
    status, body = post_action(qapp, base_of(server), "track", name="bogus")
    assert status == 400
    assert "error" in body


class _FakePlayer:
    """Minimal stand-in for the libvlc media player (audio track API only)."""

    def __init__(self):
        self._current = 1
        self.set_calls: list[int] = []

    def audio_get_track_description(self):
        return [(-1, b"Disable"), (1, b"A"), (2, b"B")]

    def audio_get_track(self):
        return self._current

    def audio_set_track(self, i):
        self.set_calls.append(i)
        self._current = i
        return 0


def test_track_toggle_with_stub_player(server, qapp):
    base = base_of(server)
    fake = _FakePlayer()
    server._controller._player = fake
    try:
        status, st = get_json(qapp, base, "/api/state")
        assert status == 200
        assert st["track"]["multi"] is True
        assert st["track"]["index"] == 0

        status, st = post_action(qapp, base, "track", name="toggle")
        assert status == 200
        assert st["track"]["multi"] is True
        assert st["track"]["index"] == 1
        assert fake.set_calls == [2]  # real track id of index 1

        status, st = post_action(qapp, base, "track", name="toggle")
        assert status == 200
        assert st["track"]["multi"] is True
        assert st["track"]["index"] == 0
        assert fake.set_calls == [2, 1]
    finally:
        server._controller._player = None


# -------------------------------------------------------------------- errors
def test_unknown_get_route_is_404(server, qapp):
    status, body = get_json(qapp, base_of(server), "/nope")
    assert status == 404
    assert "error" in body


def test_unknown_post_route_is_404(server, qapp):
    status, body = get_json_raw(
        qapp, base_of(server), "/nope", b'{"action": "clear"}'
    )
    assert status == 404


def test_bad_json_is_400(server, qapp):
    status, body = get_json_raw(qapp, base_of(server), "/api/action", b"not json{")
    assert status == 400
    assert "error" in body


def test_empty_body_is_400(server, qapp):
    status, body = request(qapp, base_of(server), "/api/action", body=b"")
    assert status == 400
    assert "error" in json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- QR helpers
def test_qr_pixmap_is_square(server, qapp):
    pix = qr_pixmap(server.url)
    assert not pix.isNull()
    assert pix.width() == pix.height()
    assert pix.width() > 100


def test_lan_ip_is_ipv4():
    assert re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", lan_ip())
