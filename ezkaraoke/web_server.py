"""LAN web server for phone-based song ordering.

A small stdlib-only HTTP service exposes the song library and the play
queue to a mobile browser on the local network. The QR code shown in the
select window encodes this service's LAN URL.

Threading model: the stdlib server runs in a daemon thread. Anything that
touches the SongDatabase or the PlayerController is dispatched to the GUI
thread through the :attr:`command` signal (queued connection); the HTTP
worker then blocks on a ``threading.Event`` until the GUI thread has
answered. The GUI thread is never blocked on the network.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap

from ezkaraoke import _segno
from ezkaraoke.database import SongDatabase
from ezkaraoke.library import Song
from ezkaraoke.player import PlayerController

DEFAULT_WEB_PORT = 8848
SEARCH_LIMIT = 50
MAX_BODY_BYTES = 1 << 20
REPLY_TIMEOUT = 5.0


def lan_ip() -> str:
    """Best-effort LAN IPv4 address of this machine (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def qr_pixmap(text: str, scale: int = 4, margin: int = 2) -> QPixmap:
    """Render *text* as a square QR code pixmap (black modules on white)."""
    symbol = _segno.make(text, error="m")
    modules = len(symbol.matrix)
    side = (modules + margin * 2) * scale
    pixmap = QPixmap(side, side)
    pixmap.fill(QColor("#ffffff"))
    painter = QPainter(pixmap)
    black = QColor("#000000")
    for y, row in enumerate(symbol.matrix):
        for x in range(modules):
            if row[x]:
                painter.fillRect(QRect((x + margin) * scale, (y + margin) * scale, scale, scale), black)
    painter.end()
    return pixmap


def _song_payload(song: Song) -> dict:
    return {"artist": song.artist, "title": song.title, "path": song.path}


def _state_payload(controller: PlayerController) -> dict:
    queue = controller.queue
    current_index = controller.current_index
    current = (
        _song_payload(queue[current_index])
        if 0 <= current_index < len(queue)
        else None
    )
    return {
        "state": controller.state,
        "current_index": current_index,
        "current": current,
        "queue": [{"index": i, **_song_payload(song)} for i, song in enumerate(queue)],
    }


def _as_index(payload: dict) -> int:
    try:
        return int(payload.get("index", -1))
    except (TypeError, ValueError):
        return -1


class WebServer(QObject):
    """Phone-ordering HTTP server bound to all interfaces.

    Must be created in the GUI thread (it lives there). :meth:`start` runs
    the stdlib server in a daemon thread; requests that touch the database
    or player are queued onto the GUI thread via :attr:`command` and
    answered through a ``threading.Event``.
    """

    command = Signal(object)

    def __init__(
        self,
        db: SongDatabase,
        controller: PlayerController,
        port: int = DEFAULT_WEB_PORT,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._controller = controller
        self._port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.command.connect(self._handle_command)

    @property
    def url(self) -> str:
        """The LAN URL phones should open (bound port, so 0 works too)."""
        port = self._httpd.server_address[1] if self._httpd is not None else self._port
        return f"http://{lan_ip()}:{port}"

    def start(self) -> bool:
        """Start serving. Returns False if the port cannot be bound."""
        if self._httpd is not None:
            return True
        try:
            self._httpd = ThreadingHTTPServer(
                ("0.0.0.0", self._port), _make_handler(self)
            )
        except OSError:
            self._httpd = None
            return False
        self._httpd.daemon_threads = True
        # Short poll interval so shutdown() (called on window close) is
        # noticed quickly — close must stay fast even with a live server.
        self._thread = threading.Thread(
            target=lambda: self._httpd.serve_forever(poll_interval=0.02),
            name="ezkaraoke-web",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._httpd = None
        self._thread = None

    # ------------------------------------------------- HTTP thread side
    def _command(self, action: str, payload: dict) -> tuple[int, bytes]:
        """Run *action* on the GUI thread; return (status, JSON bytes)."""
        env: dict = {
            "action": action,
            "payload": payload,
            "reply": threading.Event(),
            "result": None,
        }
        self.command.emit(env)
        if not env["reply"].wait(REPLY_TIMEOUT):
            return 500, b'{"error": "internal timeout"}'
        result = env["result"]
        if not isinstance(result, dict):
            result = {"error": "internal error"}
        status = 400 if "error" in result else 200
        return status, json.dumps(result, ensure_ascii=False).encode("utf-8")

    # -------------------------------------- GUI thread side (queued slot)
    def _handle_command(self, env: dict) -> None:
        try:
            env["result"] = self._dispatch(env["action"], env["payload"])
        except Exception as exc:  # noqa: BLE001 - report, don't kill the server
            env["result"] = {"error": str(exc)}
        env["reply"].set()

    def _dispatch(self, action: str, payload: dict) -> dict:
        if action == "state":
            return _state_payload(self._controller)
        if action == "search":
            songs = self._db.search(str(payload.get("q", "")))[:SEARCH_LIMIT]
            return {"results": [_song_payload(s) for s in songs]}
        if action == "add":
            song = self._db.get_song(str(payload.get("path", "")))
            if song is None:
                return {"error": "song not found"}
            was_empty = not self._controller.queue
            self._controller.append(song)
            if was_empty and self._controller.queue:
                self._controller.play_at(0)
            return _state_payload(self._controller)
        if action == "play_now":
            song = self._db.get_song(str(payload.get("path", "")))
            if song is None:
                return {"error": "song not found"}
            self._controller.play_now(song)
            return _state_payload(self._controller)
        if action == "remove":
            self._controller.remove_at(_as_index(payload))
            return _state_payload(self._controller)
        if action == "move":
            self._controller.move(_as_index(payload), int(payload.get("delta", 0) or 0))
            return _state_payload(self._controller)
        if action == "clear":
            self._controller.clear_queue()
            return _state_payload(self._controller)
        if action == "transport":
            name = str(payload.get("name", ""))
            handler = {
                "next": self._controller.next,
                "prev": self._controller.prev,
                "toggle": self._controller.toggle_pause,
                "stop": self._controller.stop,
            }.get(name)
            if handler is None:
                return {"error": "unknown transport action"}
            handler()
            return _state_payload(self._controller)
        return {"error": "unknown action"}


def _make_handler(server: WebServer):
    class Handler(_Handler):
        web = server

    return Handler


class _Handler(BaseHTTPRequestHandler):
    """Maps the phone UI's fetch calls onto :meth:`WebServer._command`."""

    web: WebServer  # set by _make_handler

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        url = urlparse(self.path)
        if url.path == "/":
            self._reply(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._reply(*self.web._command("state", {}))
        elif url.path == "/api/search":
            query = parse_qs(url.query).get("q", [""])[0]
            self._reply(*self.web._command("search", {"q": query}))
        else:
            self._reply(404, b'{"error": "not found"}')

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/api/action":
            self._reply(404, b'{"error": "not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY_BYTES:
            self._reply(400, b'{"error": "bad body"}')
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(400, b'{"error": "bad json"}')
            return
        if not isinstance(payload, dict):
            self._reply(400, b'{"error": "bad json"}')
            return
        self._reply(*self.web._command(str(payload.get("action", "")), payload))

    def _reply(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the app console/test output quiet


# --------------------------------------------------------------- phone UI
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="format-detection" content="telephone=no">
<title>EzKaraoke · 手机点歌</title>
<style>
:root { --bg:#0c0c14; --card:#1e1e2f; --fg:#ececec; --dim:#a0a0b0; --accent:#e8a030; --danger:#ff6b6b; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
html,body { margin:0; padding:0; }
body { background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif; font-size:16px; padding-bottom:calc(76px + env(safe-area-inset-bottom)); }
header { position:sticky; top:0; background:var(--bg); padding:12px 16px 8px; z-index:2; }
h1 { font-size:18px; margin:0 0 4px; }
#now { font-size:13px; color:var(--dim); min-height:18px; }
#now b { color:var(--accent); font-weight:600; }
main { padding:0 12px; max-width:640px; margin:0 auto; }
section { background:var(--card); border-radius:12px; padding:12px; margin-bottom:12px; }
h2 { font-size:13px; color:var(--dim); margin:0 0 8px; font-weight:600; }
#search { width:100%; padding:12px; font-size:16px; border-radius:10px; border:1px solid #2a2a3e; background:#151520; color:var(--fg); outline:none; }
#search:focus { border-color:var(--accent); }
#results { list-style:none; margin:8px 0 0; padding:0; display:none; }
#results li { display:flex; align-items:center; gap:8px; padding:10px 8px; border-bottom:1px solid #2a2a3e; }
#results li:last-child { border-bottom:none; }
.name { flex:1; min-width:0; }
.t { font-size:15px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.a { font-size:12px; color:var(--dim); }
button { min-height:44px; }
.addbtn { flex:0 0 auto; background:var(--accent); color:#0c0c14; border:none; border-radius:8px; padding:10px 16px; font-size:14px; font-weight:700; }
#queue { list-style:none; margin:0; padding:0; }
#queue li { display:flex; align-items:center; gap:8px; padding:8px; border-radius:10px; }
#queue li.current { background:#2a2a3e; }
.idx { flex:0 0 24px; text-align:center; color:var(--dim); font-size:13px; }
#queue li.current .idx { color:var(--accent); font-weight:700; }
.op { flex:0 0 auto; background:#2a2a3e; color:var(--fg); border:none; border-radius:8px; width:36px; height:36px; font-size:16px; line-height:1; padding:0; }
.op.danger { color:var(--danger); }
.op:disabled { opacity:.35; }
.empty { color:var(--dim); font-size:14px; text-align:center; padding:12px 0; }
nav { position:fixed; bottom:0; left:0; right:0; background:#151520; border-top:1px solid #2a2a3e; display:flex; justify-content:space-around; padding:8px 8px calc(8px + env(safe-area-inset-bottom)); z-index:3; }
nav button { flex:1; margin:0 4px; background:#2a2a3e; color:var(--fg); border:none; border-radius:10px; padding:10px 0; font-size:14px; }
nav button.primary { background:var(--accent); color:#0c0c14; font-weight:700; }
nav button.warn { color:var(--danger); }
#toast { position:fixed; left:50%; bottom:100px; transform:translateX(-50%); background:#2a2a3e; color:var(--fg); padding:10px 18px; border-radius:20px; font-size:14px; opacity:0; transition:opacity .25s; pointer-events:none; z-index:4; white-space:nowrap; }
#toast.show { opacity:1; }
</style>
</head>
<body>
<header>
  <h1>EzKaraoke · 手机点歌</h1>
  <div id="now">未在播放</div>
</header>
<main>
  <section>
    <h2>搜索点歌</h2>
    <input id="search" type="search" placeholder="输入歌手或歌名" autocomplete="off">
    <ul id="results"></ul>
  </section>
  <section>
    <h2>播放队列</h2>
    <ul id="queue"></ul>
    <div id="qempty" class="empty">队列为空，搜索后点「＋ 点歌」加入</div>
  </section>
</main>
<nav>
  <button id="b-prev">⏮ 上一首</button>
  <button id="b-toggle" class="primary">▶ 播放</button>
  <button id="b-next">下一首 ⏭</button>
  <button id="b-clear" class="warn">清空</button>
</nav>
<div id="toast"></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  try { return await r.json(); } catch (e) { return {}; }
}
function post(payload) {
  return api("/api/action", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) });
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 1600);
}

function render(state) {
  if (!state) return;
  const now = $("now");
  if (state.current) {
    const mark = state.state === "playing" ? "▶" : state.state === "paused" ? "⏸" : "";
    now.innerHTML = mark + " 正在播放：<b>" + esc(state.current.title) + "</b> · " + esc(state.current.artist);
  } else {
    now.textContent = "未在播放";
  }
  $("b-toggle").textContent = state.state === "playing" ? "⏸ 暂停" : "▶ 播放";

  const ul = $("queue"); ul.innerHTML = "";
  const n = (state.queue || []).length;
  for (const s of state.queue || []) {
    const li = document.createElement("li");
    if (s.index === state.current_index) li.className = "current";
    li.innerHTML =
      '<span class="idx">' + (s.index === state.current_index ? "▶" : s.index + 1) + "</span>" +
      '<span class="name"><div class="t">' + esc(s.title) + '</div><div class="a">' + esc(s.artist) + "</div></span>";
    const ops = [
      ["up", "↑", s.index === 0, () => post({action:"move", index:s.index, delta:-1})],
      ["down", "↓", s.index === n - 1, () => post({action:"move", index:s.index, delta:1})],
      ["del", "✕", false, () => {
        if (s.index === state.current_index && !confirm("删除正在播放的歌曲？")) return;
        return post({action:"remove", index:s.index});
      }],
    ];
    for (const [, label, disabled, fn] of ops) {
      const b = document.createElement("button");
      b.className = "op" + (label === "✕" ? " danger" : "");
      b.textContent = label; b.disabled = !!disabled;
      b.onclick = async () => { await fn(); refresh(); };
      li.appendChild(b);
    }
    ul.appendChild(li);
  }
  $("qempty").style.display = n ? "none" : "block";
}

async function refresh() {
  const s = await api("/api/state");
  render(s);
}

let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 250);
});
async function runSearch() {
  const q = $("search").value.trim();
  const d = await api("/api/search?q=" + encodeURIComponent(q));
  const ul = $("results"); ul.innerHTML = "";
  for (const s of (d.results || []).slice(0, 30)) {
    const li = document.createElement("li");
    li.innerHTML = '<span class="name"><div class="t">' + esc(s.title) + '</div><div class="a">' + esc(s.artist) + "</div></span>";
    const b = document.createElement("button");
    b.className = "addbtn"; b.textContent = "＋ 点歌";
    b.onclick = async () => {
      const r = await post({action:"add", path:s.path});
      toast(r.error ? "加入失败" : "已加入队列");
      $("search").value = ""; ul.innerHTML = ""; ul.style.display = "none";
      refresh();
    };
    li.appendChild(b);
    ul.appendChild(li);
  }
  ul.style.display = (d.results || []).length ? "block" : "none";
}

$("b-prev").onclick = async () => { await post({action:"transport", name:"prev"}); refresh(); };
$("b-next").onclick = async () => { await post({action:"transport", name:"next"}); refresh(); };
$("b-toggle").onclick = async () => { await post({action:"transport", name:"toggle"}); refresh(); };
$("b-clear").onclick = async () => { if (!confirm("清空整个播放队列？")) return; await post({action:"clear"}); refresh(); };

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
