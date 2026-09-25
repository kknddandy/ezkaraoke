"""Lightweight en/zh UI translations.

Chinese is the source language: in ``zh`` mode :func:`tr` returns its key
unchanged, so only the English table is spelled out and the two languages
can never drift apart structurally. Keys are the Chinese source strings.
Data values (artist names, titles, file paths) are never keys and pass
through untouched.
"""

from __future__ import annotations

from collections.abc import Callable

SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh", "en")
DEFAULT_LANGUAGE = "zh"

_EN: dict[str, str] = {
    # ---- window titles
    "ezkaraoke · 点歌台": "ezkaraoke · Song Selector",
    "ezkaraoke · 播放器": "ezkaraoke · Player",
    # ---- select window
    "歌手": "Artist",
    "首字母": "First Letter",
    "选择文件夹…": "Choose Folder…",
    "重新扫描": "Rescan",
    "搜索歌手或歌名…": "Search artist or title…",
    "歌名": "Title",
    "版本": "Version",
    "文件尺寸": "Size",
    "序号": "#",
    "点歌": "Queue",
    "插入播放": "Insert",
    "立即播放": "Play Now",
    "播放": "Play",
    "暂停": "Pause",
    "原唱/伴奏": "Vocal/Karaoke",
    "切换当前歌曲的音轨（原唱/伴奏）": (
        "Switch the current song's audio track (vocal/karaoke)"
    ),
    "当前音轨：原唱": "Current track: vocal",
    "当前音轨：伴奏": "Current track: karaoke",
    "插歌": "Play Next",
    "把选中的歌曲移到正在播放歌曲的下一首": (
        "Move the selected song to play right after the current one"
    ),
    # ---- phone ordering (QR + web service)
    "手机扫码点歌": "Scan QR to order songs",
    "手机点歌服务启动失败（端口 {port} 被占用）": (
        "Phone ordering unavailable (port {port} is in use)"
    ),
    "上移": "Up",
    "下移": "Down",
    "删除": "Remove",
    "红心入队": "Queue ♥",
    "把所有红心歌曲加入队列": "Add every ♥ song to the queue",
    "没有红心歌曲": "No ♥ songs yet",
    "红心歌曲都已在队列中": "All ♥ songs are already queued",
    "已加入 {count} 首红心歌曲": "Queued {count} ♥ songs",
    "未设置文件夹": "No folder set",
    "选择音乐文件夹": "Choose Music Folder",
    "文件夹不存在": "Folder does not exist",
    "请设置音乐文件夹": "Please set a music folder",
    "请先设置音乐文件夹": "Please set a music folder first",
    "文件夹无效": "Invalid folder",
    "正在扫描音乐文件夹…": "Scanning music folder…",
    "扫描错误: {message}": "Scan error: {message}",
    "共 {count} 首": "{count} songs",
    "共 {count} 首（本地数据库）": "{count} songs (local database)",
    "共 {count} 首歌曲": "{count} songs",
    "全部 ({count})": "All ({count})",
    "正在获取歌手头像": "Fetching artist avatars",
    "永久删除": "Delete Permanently",
    "从磁盘删除视频文件：{path}": "Delete the video file from disk: {path}",
    "确定要永久删除这首歌曲吗？\n\n{display}\n\n将删除视频文件：\n{path}\n\n此操作不可撤销。": (
        "Permanently delete this song?\n\n{display}\n\n"
        "The video file will be deleted:\n{path}\n\nThis cannot be undone."
    ),
    "删除文件失败：{error}": "Failed to delete file: {error}",
    "已永久删除《{title}》": "Permanently deleted: {title}",
    # ---- player window
    "上一首": "Previous",
    "下一首": "Next",
    "重播": "Replay",
    "重新播放当前歌曲": "Replay the current song from the beginning",
    "降调": "Key Down",
    "升调": "Key Up",
    "原调": "Original",
    "升{semis}": "+{semis}",
    "降{semis}": "-{semis}",
    "生成{pct}%": "Generating {pct}%",
    "该歌曲没有可切换的音轨": "This song has no switchable audio tracks",
    "未在播放": "Not playing",
    "播放中": "Playing",
    "已暂停": "Paused",
    "全屏": "Fullscreen",
    "全屏 (F11)": "Fullscreen (F11)",
    "退出全屏": "Exit Fullscreen",
    "警告：无法设置视频输出（请确认 VLC 已安装）": (
        "Warning: cannot set up video output (make sure VLC is installed)"
    ),
    "纯变调（rubberband，首次使用需后台生成，约 1 分钟）": (
        "Pure pitch shift (rubberband; the first use is generated in the "
        "background, about 1 minute)"
    ),
    "缺少 ffmpeg：变调同时改变速度": "ffmpeg missing: pitch shift also changes tempo",
    # ---- loudness normalization
    "响度对齐": "Loudness",
    "正在测量响度": "Measuring loudness",
    "开始测量": "Start measuring",
    "停止测量": "Stop measuring",
    "停止": "Stopped",
    "重新测量全部": "Re-measure all",
    "启用": "Enable",
    "停用": "Disable",
    "目标响度": "Target loudness",
    "已测 {ok} / {total}（失败 {failed}）": "Measured {ok}/{total} ({failed} failed)",
    "目标 {target} LUFS": "Target {target} LUFS",
    "确定重新测量全部歌曲的响度吗？": "Re-measure loudness for all songs?",
    # ---- status messages (emitted as raw keys by PlayerController)
    "未检测到 VLC 运行库，请安装 VLC 后重启": (
        "VLC runtime not detected. Install VLC and restart"
    ),
    "已切换为原唱": "Switched to the vocal track",
    "已切换为伴奏": "Switched to the karaoke track",
    "变调生成失败（ffmpeg），保持原曲播放": (
        "Pitch shift generation failed (ffmpeg); keeping the original"
    ),
    "未找到 ffmpeg，无法测量响度": "ffmpeg not found; cannot measure loudness",
}

_language: str = DEFAULT_LANGUAGE
_listeners: list[Callable[[], None]] = []


def current_language() -> str:
    return _language


def set_language(language: str, *, notify: bool = True) -> None:
    """Switch the active language and notify registered listeners.

    Unknown values fall back to :data:`DEFAULT_LANGUAGE`. Listeners are
    not re-notified when the language is unchanged.
    """
    global _language
    language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    if language == _language:
        return
    _language = language
    if notify:
        for callback in list(_listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001 - one broken listener must not block the rest
                pass


def on_language_changed(callback: Callable[[], None]) -> None:
    """Register *callback* to run whenever the language changes."""
    _listeners.append(callback)


def off_language_changed(callback: Callable[[], None]) -> None:
    try:
        _listeners.remove(callback)
    except ValueError:
        pass


def tr(text: str, **kwargs) -> str:
    """Translate *text* into the active language.

    Unknown keys (song data, foreign error strings) pass through as-is.
    *kwargs* are substituted with ``str.format``.
    """
    out = _EN.get(text, text) if _language == "en" else text
    if kwargs:
        try:
            out = out.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return out
