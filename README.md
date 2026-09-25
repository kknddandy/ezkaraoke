# ezkaraoke

A local KTV karaoke player for home use. Point it at a folder of video
songs, build a searchable song library, and sing along with
original/instrumental track switching and per-song pitch shift.

- PySide6 (Qt for Python) desktop app, videos played through VLC
- Song library stored in local SQLite — instant startup, no rescanning
- Artist avatars fetched once and cached locally
- Bilingual UI: Chinese (default) and English, switchable at runtime

---

## Features

- **Local song library** — scans a music folder once and builds a
  singer → songs index in a local SQLite database; the app starts in
  milliseconds afterwards. "Rescan" rebuilds the library any time.
- **Classic request-podium layout** — artists on the left, song table in
  the middle, play queue on the right.
- **Two browse modes** — by artist (with cached avatars) or by first
  letter of the title (pinyin A–Z, empty letters greyed out).
- **Live search** — filter by artist or title as you type.
- **Pinyin-sorted table** — click the Artist / Title header to sort by
  pinyin, click again to reverse. Sub-0.1 s even with thousands of songs.
- **Request / insert / play now** — add songs to the end of the queue,
  insert after the current song, or jump straight to one song.
- **Queue management** — move up/down, remove, double-click to play.
- **Phone ordering** — a QR code on the podium points phones at a local
  web page (port 8848): search songs, add them to the queue, and control
  playback from any device on the network. LAN-only by design: the page
  has no authentication, so use it only on a trusted home network.
- **Pitch shift (semitones, ±12)** — per-song key change; the preference
  is remembered across songs. VLC ≥ 4 does a pure pitch shift; with
  VLC < 4 the tempo changes as well.
- **Original / instrumental switching** — videos with two audio tracks
  can be flipped between vocal and karaoke track in one click, synced
  between the player and the request podium.
- **Fullscreen player** — borderless fullscreen with auto-hiding
  controls (F11, Esc, or double-click the video).
- **Bilingual UI** — Chinese (default) and English; switch at runtime
  from the language button on the toolbar.
- **Background jobs** — scanning and avatar downloads run off the main
  thread with a live progress bar.
- **Dark KTV theme** — immersive dark UI with warm accents.

## File naming convention

The scanner recognises video files named:

```
Artist-Title-anything.ext
```

e.g. `Jay Chou-Sunny Day-OfficialMV.mp4` (or
`周杰伦-晴天-OfficialMV.mp4`). The first `-` separates artist from
title; everything after the second `-` is ignored. Files without an
artist part are filed under "Unknown Artist".

## Requirements

- Python 3.10+ (not needed for the standalone Windows build)
- PySide6 6.6+ (provided by system packages when installing the .deb)
- VLC media player (video decoding), installed system-wide:
  - Debian/Ubuntu: `sudo apt install vlc`
  - Windows: <https://www.videolan.org/vlc/> (64-bit build)
  - macOS: `brew install --cask vlc`
- Optional: `ffmpeg` built with the `rubberband` filter for *pure* pitch
  shift (without it, pitch shift also changes tempo)

Runs on Linux (X11/Wayland), Windows 10/11 and macOS.

## Install & run

### Windows

From source (no packaging needed):

```powershell
git clone https://github.com/kknddandy/ezkaraoke; cd ezkaraoke
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m ezkaraoke
```

Standalone build (no Python required on the target machine):

```powershell
python -m pip install -e ".[dev]" pyinstaller
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

The result is `dist\ezkaraoke\ezkaraoke.exe`; copy the whole folder to the
target machine. VLC must still be installed there.

### Option 1: .deb package (recommended, Debian/Ubuntu)

```bash
./packaging/build_deb.sh                       # build (needs dpkg-deb)
sudo apt install ./dist/ezkaraoke_0.2.0_amd64.deb
ezkaraoke                                       # or use the desktop menu entry
```

### Option 2: from source (Linux/macOS)

```bash
git clone https://github.com/kknddandy/ezkaraoke && cd ezkaraoke
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # or: pip install -r requirements.txt
python -m ezkaraoke
```

### Option 3: pip

```bash
pip install git+https://github.com/kknddandy/ezkaraoke.git
ezkaraoke
```

User data (not part of the package) is stored per platform:

| OS      | Config (`config.json`)  | Song library + pitch cache |
| ------- | ----------------------- | -------------------------- |
| Linux   | `~/.config/ezkaraoke/`  | `~/.local/share/ezkaraoke/` |
| Windows | `%APPDATA%\ezkaraoke\`  | `%LOCALAPPDATA%\ezkaraoke\` |
| macOS   | `~/Library/Application Support/ezkaraoke/` | same |

The config holds the music folder, language and phone-ordering web port.

## Usage

1. In the request podium click **Choose Folder…** and pick the folder
   that holds your video songs. Scanning runs in the background with a
   progress bar; **Rescan** rebuilds the library at any time.
2. Browse by artist or first letter on the left, or type in the search
   box. Click the Artist/Title header to pinyin-sort the table.
3. Select songs and press **Queue** (append), **Insert** (after the
   current song) or **Play Now**.
4. Reorder or remove songs in the queue on the right; double-click a
   queue row to play it immediately.
5. Phone ordering: scan the QR code at the top of the queue panel with a
   phone on the same network. The web page shows the current queue and
   lets you search and add songs (the first song added to an empty queue
   starts playback) and control next / pause / clear. If port 8848 is
    busy the panel reports it and the desktop UI keeps working. The web
    page is for a trusted home LAN only — it has no authentication.
6. Player window:
    - `Space` play/pause
    - `F11`, the fullscreen button, or a double-click on the video
      toggles borderless fullscreen; `Esc` exits. Controls auto-hide
      after ~2.5 s of inactivity and reappear when you move the mouse.
    - **Key Down / Key Up** shift the pitch by semitones (label shows
      Original / +N / −N).
    - **Vocal/Karaoke** switches the audio track of dual-track videos.
7. Language: the toolbar button (shows the *other* language) switches
   the whole UI between Chinese and English instantly; the choice is
   saved and restored on the next start.

## Video output

- **Windows / X11 / XWayland** — hardware-accelerated direct window output
  (`set_hwnd`).
- **Native Wayland** — automatic software-render fallback (VLC decodes,
  frames are read back and drawn in Qt). Slightly higher CPU usage,
  full functionality.

If the app reports that VLC is missing, install VLC for your platform
(see [Requirements](#requirements)) and restart.

## Loudness normalization

Songs in a home library come from different eras and masters, so their
loudness varies widely. On the reference library (15,237 files /
1.1 TB) the measured integrated loudness has a median of **−11.25 LUFS**
but spans **−31.2 to −7.6 LUFS** (a ~24 dB spread): some songs are
deafening, others nearly inaudible, and users had to ride the volume
slider between songs.

ezkaraoke evens this out **without ever modifying the media files**:

- **One-off background measurement.** Every audio track of every song is
  measured once with `ffmpeg ... -vn -af ebur128=peak=true` (audio only —
  no video decoding). Results are cached in the SQLite library
  (`loudness` table, keyed by path + track position, with a size/mtime
  snapshot so replaced files are re-measured automatically). Strictly
  read-only: media files are never touched.
- **Static playback gain.** During playback a single static gain is
  applied via `AudioEqualizer.set_preamp` (libvlc 3.x, ±20 dB, pure
  gain — no tonal change), bringing the track toward a configurable
  target loudness (default −11.25 LUFS). The volume slider remains
  exclusively yours.
- **Toggleable** for A/B comparison — turning it off restores the
  original, unadjusted behaviour.

CLI (no GUI required; handy for measuring a full library on a dev
machine first):

```bash
python -m ezkaraoke.loudness --scan /mnt/NAS/Karaoke --workers 8
python -m ezkaraoke.loudness --stats        # statistics only, no measuring
```

Config: `loudness_enabled` (default on), `loudness_target` (−30 to
−5 LUFS, default −11.25), `loudness_workers` (0 to 16; 0 = auto,
cpu/2; default 8).

Measurement averages ≈ 98× real time with `-vn`; the full 15,237-file /
1.1 TB library takes ≈ 1.7 h with 8 parallel workers (warm NAS cache).

## Project layout

```
ezkaraoke/
  main.py            entry point, builds config + both windows
  select_window.py   request-podium window (browse, queue, actions)
  player_window.py   player window (video surface, controls, fullscreen)
  player.py          VLC queue/state controller (pitch, tracks)
  scanner.py         filename parser + folder scanner
  database.py        SQLite song library
  letters.py         pinyin first-letter indexing
  avatar.py          artist avatar fetch/cache
  pitchshift.py      ffmpeg/rubberband pure pitch shift helpers
  frame_bridge.py    Wayland software video output bridge
  i18n.py            en/zh UI translations
  config.py          JSON config (music folder, language, web port)
  paths.py           per-platform config/data directories
  web_server.py      phone-ordering HTTP server + QR pixmap + web page
  _segno/            vendored segno 1.6.6 QR encoder (BSD)
packaging/           .deb + Windows build scripts, desktop entry, icons
tests/               pytest suite (offscreen Qt)
```

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

The UI tests run with `QT_QPA_PLATFORM=offscreen` (set automatically in
`tests/conftest.py`), so no display is required.

## Known limitations

Loudness normalization is deliberately simple — a static gain applied at
playback time, no re-encoding. Its trade-offs:

1. **Boosts above +12 dB amplify the noise floor.** Some old tape
   sources (e.g. the −31 LUFS ones) become audibly hissy when brought
   up. The boost is therefore capped at +12 dB, and it is preferred to
   under-boost a song than to amplify its noise.
2. **Per-song internal dynamics are not addressed.** Static gain
   equalises songs against each other, not the level swings within a
   song (the library contains tracks with a loudness range up to
   19.8 LU). Fixing that would require dynamic compression, which
   damages music.
3. **Only effective inside ezkaraoke.** The gain is applied by this
   player at playback time; other players see the original loudness.
   This is the core trade-off of not re-encoding (zero risk, takes
   effect immediately, can be reversed at any time).
4. **Measurements can go stale.** Cached values are invalidated only by
   the size/mtime snapshot: a replacement file with the same size and
   mtime reuses the old value. Known and accepted.
5. **VLC 4 upgrade note.** The ±20 dB range is the libvlc 3.x preamp
   convention (tested on VLC 3.0.23). When upgrading to libvlc 4,
   re-verify the preamp bounds and update the clamp constants.
6. **NAS bandwidth during measurement.** 8 parallel measurements
   saturate a gigabit NAS link; if you are singing while the library is
   being measured, lower `loudness_workers` (e.g. 2–3).

## License

Apache-2.0 — see [LICENSE](LICENSE). The .deb build vendors a
single-file copy of [python-vlc](https://pypi.org/project/python-vlc/)
(`packaging/vendor/`, MIT) because Ubuntu does not package it. QR
encoding uses [segno](https://pypi.org/project/segno/) 1.6.6 (BSD),
vendored at `ezkaraoke/_segno/`.

---

# ezkaraoke（中文）

家用 KTV 点歌系统 — 简单易用的家庭卡拉 OK 解决方案。

- PySide6（Qt for Python）桌面应用，视频通过 VLC 播放
- 曲库保存在本地 SQLite — 秒开启动，无需重复扫描
- 歌手头像只获取一次并本地缓存
- 界面双语：中文（默认）/ 英文，运行时可切换

## 功能特性

- **本地曲库管理**：扫描一次指定文件夹，构建歌手-歌曲索引并存入本地
  SQLite；之后启动秒开。可随时 **重新扫描** 重建。
- **经典点歌台界面**：左侧歌手列表 / 中间歌曲浏览 / 右侧播放队列。
- **两种浏览模式**：按歌手（带头像，首次启动自动从网络获取并缓存）或
  按歌名拼音首字母 A–Z（无歌曲的字母置灰）。
- **实时搜索**：输入即按歌手或歌名过滤。
- **歌曲表拼音排序**：点击"歌手"/"歌名"表头按拼音排序，再点一次反转；
  另有"文件尺寸"列显示视频大小并可点击按字节数排序；
  万首级曲库切换/排序均 < 0.1s。
- **点歌 / 插入播放 / 立即播放**：加入队列末尾、插到当前歌曲之后、或
  直接播放。
- **队列管理**：上移、下移、删除，双击队列歌曲立即切歌。
- **手机扫码点歌**：点歌台队列面板顶部的二维码指向局域网网页
  （端口 8848），手机可搜索点歌、查看队列并控制播放。该网页仅限可信的
  家庭局域网使用——页面没有任何身份验证，请勿将 8848 端口暴露到公网。
- **升调/降调**：半音步进变调（±12 半音，标签显示 原调/升N/降N），
  偏好跨歌曲保留；VLC ≥ 4 为纯变调，VLC < 4 变调的同时会改变速度。
- **原唱/伴奏切换**：双音轨视频一键切换原唱与伴奏，播放窗口与点歌台
  同步，偏好跨歌曲保留。
- **全屏播放**：无边框全屏，控制栏自动隐藏（F11 / Esc / 双击视频切换）。
- **界面双语**：中文（默认）/ 英文，工具栏按钮一键切换，选择会被保存。
- **后台任务进度提示**：扫描与头像下载在后台线程执行，状态栏进度条
  实时显示，不阻塞界面。
- **深色 KTV 主题**：沉浸式暗色界面，搭配暖色点缀。

## 文件名约定

扫描器按以下格式识别歌曲文件：

```
歌手-歌曲名称-xxxxx.xxx
```

例如：`周杰伦-晴天-OfficialMV.mp4`。第一个 `-` 分隔歌手与歌名，第二个
`-` 之后的附加信息会被忽略。缺少歌手部分的文件归入"未知歌手"。

## 环境要求

- Python 3.10+（Windows 独立打包版无需 Python）
- PySide6 6.6+（.deb 安装时由系统包自动满足）
- VLC 媒体播放器（用于视频解码），需系统级安装：
  - Debian/Ubuntu：`sudo apt install vlc`
  - Windows：<https://www.videolan.org/vlc/>（64 位版）
  - macOS：`brew install --cask vlc`
- 可选：带 `rubberband` 滤镜的 `ffmpeg`，用于纯变调（没有则变调会同时
  改变速度）

支持 Linux（X11/Wayland）、Windows 10/11 与 macOS。

## 安装与运行

### Windows

从源码运行（无需打包）：

```powershell
git clone https://github.com/kknddandy/ezkaraoke; cd ezkaraoke
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m ezkaraoke
```

打包为独立 exe（目标机器无需 Python）：

```powershell
python -m pip install -e ".[dev]" pyinstaller
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

生成 `dist\ezkaraoke\ezkaraoke.exe`，把整个文件夹复制到目标机器即可，
目标机器仍需安装 VLC。

### 方法一：安装 .deb 包（推荐，Debian/Ubuntu）

```bash
./packaging/build_deb.sh                       # 构建（需要 dpkg-deb）
sudo apt install ./dist/ezkaraoke_0.2.0_amd64.deb
ezkaraoke                                       # 或从桌面菜单启动
```

### 方法二：从源码运行（Linux/macOS）

```bash
git clone https://github.com/kknddandy/ezkaraoke && cd ezkaraoke
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # 或 pip install -r requirements.txt
python -m ezkaraoke
```

### 方法三：pip 安装

```bash
pip install git+https://github.com/kknddandy/ezkaraoke.git
ezkaraoke
```

用户数据（不在包内）按平台存放：

| 系统    | 配置（`config.json`）   | 曲库 + 变调缓存            |
| ------- | ----------------------- | -------------------------- |
| Linux   | `~/.config/ezkaraoke/`  | `~/.local/share/ezkaraoke/` |
| Windows | `%APPDATA%\ezkaraoke\`  | `%LOCALAPPDATA%\ezkaraoke\` |
| macOS   | `~/Library/Application Support/ezkaraoke/` | 同上 |

配置内容：音乐文件夹、界面语言、手机点歌网页端口。

## 使用说明

1. 在点歌台窗口点击 **选择文件夹…**，指定存放歌曲视频的文件夹；扫描
   在后台执行并显示进度条，可随时 **重新扫描** 重建曲库。
2. 左侧按歌手或首字母浏览，或用搜索框输入关键词；点击"歌手"/"歌名"
   表头可按拼音排序（再点一次反转）。
3. 选中歌曲后点击 **点歌**（追加）、**插入播放**（插到当前歌曲之后）
   或 **立即播放**。
4. 右侧播放队列中可上移/下移/删除歌曲，双击队列行立即切歌。
5. 手机扫码点歌：用同一局域网内的手机扫描队列面板顶部的二维码，网页
   可显示当前队列、搜索点歌（空队列中第一首会自动开始播放）并控制
   上一首/播放暂停/下一首/清空。若 8848 端口被占用，面板会提示，
   桌面端功能不受影响。该网页仅限可信的家庭局域网使用，没有任何身份验证。
6. 播放器窗口：
   - `Space` 播放/暂停
   - `F11`、全屏按钮或双击视频切换无边框全屏；`Esc` 退出；约 2.5 秒
     无操作后控制栏自动隐藏，移动鼠标重新显示
   - **降调/升调** 以半音为单位变调（标签显示 原调/升N/降N）
    - **原唱/伴奏** 切换双音轨视频的音轨
7. 语言：工具栏按钮显示"另一种语言"（中文模式下显示 EN），点击即时
    切换整个界面，选择会被保存并在下次启动时恢复。

## 视频输出策略

- **Windows / X11 / XWayland**：窗口句柄硬件加速直出（`set_hwnd`）。
- **原生 Wayland**：自动切换软件渲染模式（VLC 解码后回读帧到 Qt
  显示，CPU 占用略高，功能完整）。

若提示缺少 VLC，请按 [环境要求](#环境要求) 安装对应平台的 VLC 后重启。

## 响度对齐

曲库里的歌曲来自不同年代、不同母带，响度参差不齐。参考曲库
（15,237 个文件 / 1.1 TB）实测：集成响度中位数 **−11.25 LUFS**，
全距达 **−31.2 ~ −7.6 LUFS**（约 24 dB 跨度）——有的歌震耳、有的
几乎听不见，用户必须手动推音量滑杆跟着调。

ezkaraoke 在**绝不修改媒体文件**的前提下解决该问题：

- **一次性后台测量**：对每个文件的每条音轨各测量一次，命令为
  `ffmpeg ... -vn -af ebur128=peak=true`（只解音频，不解视频）。
  结果缓存进 SQLite（`loudness` 表，以路径 + 音轨序号为主键，并记录
  测量时的大小/mtime 快照；文件被替换后会自动重测）。严格只读，
  从不写入媒体文件。
- **播放时静态增益**：播放时通过 `AudioEqualizer.set_preamp` 施加单一
  静态增益（libvlc 3.x，±20 dB，纯增益、不改变音色），把当前音轨
  对齐到可配置的目标响度（默认 −11.25 LUFS）。音量滑杆仍由用户
  独占。
- **可随时开关**，便于 A/B 对比；关闭后行为与改造前完全一致。

CLI（不依赖 GUI，适合先在开发机上跑全库）：

```bash
python -m ezkaraoke.loudness --scan /mnt/NAS/Karaoke --workers 8
python -m ezkaraoke.loudness --stats        # 只打印统计，不测量
```

配置项：`loudness_enabled`（默认开启）、`loudness_target`
（−30 ~ −5 LUFS，默认 −11.25）、`loudness_workers`（0 ~ 16；
0 = 自动取 cpu/2，默认 8）。

`-vn` 下测量速度平均约 98× 实时；15,237 个文件 / 1.1 TB 的曲库
8 并行约 1.7 小时（NAS 热缓存）。

## 项目结构

```
ezkaraoke/
  main.py            入口：加载配置并创建两个窗口
  select_window.py   点歌台窗口（浏览、队列、点歌操作）
  player_window.py   播放窗口（视频画面、控制栏、全屏）
  player.py          VLC 队列/状态控制器（变调、音轨切换）
  scanner.py         文件名解析 + 文件夹扫描
  database.py        SQLite 曲库
  letters.py         拼音首字母索引
  avatar.py          歌手头像获取/缓存
  pitchshift.py      ffmpeg/rubberband 纯变调辅助
  frame_bridge.py    Wayland 软件视频输出桥
  i18n.py            中/英界面翻译
  config.py          JSON 配置（音乐文件夹、语言、网页端口）
  paths.py           各平台配置/数据目录
  web_server.py      手机点歌 HTTP 服务 + 二维码 + 网页
  _segno/            内置的 segno 1.6.6 二维码编码库（BSD）
packaging/           .deb 与 Windows 构建脚本、桌面条目、图标
tests/               pytest 测试（offscreen Qt）
```

## 开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

UI 测试通过 `QT_QPA_PLATFORM=offscreen` 运行（`tests/conftest.py` 已
自动设置），无需显示器。

## 已知限制

响度对齐刻意保持简单——静态增益、播放时施加、不重编码——相应有
以下取舍：

1. **提升超过 +12 dB 会把底噪一起放大**：库里有 −31 LUFS 的老录像带
   源，这类文件对齐后"响度够了但嘶声明显"。因此提升上限为 +12 dB，
   超出部分不补——宁可偏小，不要变脏。
2. **单曲内部的动态范围不对齐**：静态增益只能对齐"歌曲之间"，无法
   对齐"一首歌内部"的起伏（库内 LRA 最高 19.8 LU）。要解决需要动态
   压缩，会破坏音乐，本方案不做。
3. **只在 ezkaraoke 内生效**：增益在播放时由本播放器施加，其他播放器
   播放同一文件仍是原始响度。这是"不改文件"的核心权衡（零风险、
   秒级生效、可随时反悔）。
4. **旧测量值可能被复用**：缓存以 size/mtime 快照判定失效；若替换
   文件的大小与 mtime 都保持不变，将沿用旧值——已知且可接受。
5. **VLC 4 升级注意**：±20 dB 是 libvlc 3.x 的 preamp 约定（本项目
   在 VLC 3.0.23 上实测）。升级到 libvlc 4 时需重新确认 preamp 边界
   并更新夹取常量。
6. **NAS 带宽**：8 并行测量会占满千兆网口；测量期间若同时在唱，
   建议把 `loudness_workers` 调低到 2~3。

## 许可证

Apache-2.0 — 见 [LICENSE](LICENSE)。.deb 构建内置了一份
[python-vlc](https://pypi.org/project/python-vlc/) 单文件副本
（`packaging/vendor/`，MIT），因为 Ubuntu 未打包该库。二维码编码使用
[segno](https://pypi.org/project/segno/) 1.6.6（BSD），内置于
`ezkaraoke/_segno/`。
