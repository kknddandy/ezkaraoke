# ezkaraoke 全库响度对齐（Loudness Normalization）需求方案 v1.0

> 交给 opencode 开发。所有「实测」数据都是在目标机器 + 目标库上真实跑出来的，可直接作为验收基线。
> 本文档不含待定项：所有需要决策的地方都已给出结论与理由；如遇实现冲突，以本文档的「非目标」和「关键决策」为准。

---

## 1. 背景与问题

自建卡拉OK库：`/mnt/NAS/Karaoke/`，**15,237 个文件 / 1.1 TB**。

实测响度分布（随机抽样 16 个文件，`ffmpeg ebur128` 只解音频）：

| 指标 | 数值 |
|---|---|
| 中位数 | **−11.25 LUFS** |
| 主簇 | −9 ~ −12 LUFS |
| 全距 | **−31.2 ~ −7.6 LUFS**（约 24 dB 跨度） |

**问题**：连续播放不同歌曲时，有的震耳、有的几乎听不见，用户必须手动调音量滑杆。

**根因**：文件来自不同年代、不同母带、不同压制来源，响度并未统一。这不是播放器的问题，是**元数据缺失**——软件不知道每首歌有多大声。

---

## 2. 目标 / 非目标

### 2.1 目标

1. **一次性后台测量**全库每个文件（每个音轨）的响度，结果缓存进 SQLite。**只读，绝不修改媒体文件。**
2. **播放时**按测量值施加一个静态增益，使感知响度一致（目标 −11.25 LUFS）。
3. 新入库/被替换的文件**自动补测**（幂等、断点续测）。
4. 提供开关，可随时关闭以做 A/B 对比。
5. 提供一个**不依赖 GUI 的 CLI 入口**，便于先在开发机上跑全库。

### 2.2 非目标（明确不要做的事）

| 不要做 | 理由 |
|---|---|
| ❌ 批量重编码媒体文件 | 1.1 TB 重写、NAS 仅剩 1.5 TB 无法备份、不可逆、耗时十几个小时 |
| ❌ 用 VLC 的 `normvol` / `compressor` 音频滤镜做实时规整 | 那是对音乐做动态压缩，会「泵动」；库内 LRA 最高达 19.8 LU，听感会很糟 |
| ❌ 用 `audio_set_volume` 做补偿 | VLC 3 上限 200%（+6 dB），覆盖不了 −31 LUFS 需要的 +20 dB；且会劫持用户的音量滑杆语义 |
| ❌ 做跨曲淡入淡出 / 变速对齐 | 与本需求无关 |
| ❌ 多遍 `loudnorm` 双通处理 | 静态增益 + 削波保护已经足够，双通会引入动态压缩 |

---

## 3. 关键决策（已定，附实测依据）

### D1：运行时增益，用 `AudioEqualizer.set_preamp`，不用 volume

libvlc 3.0.23（本机实际版本，已用 `vlc.libvlc_get_version()` 确认）实测：

```
set_preamp(35.0) → get_preamp() = 20.0     # 上限夹到 +20 dB
set_preamp(-35.0) → get_preamp() = -20.0   # 下限夹到 -20 dB
新建 AudioEqualizer 默认 preamp = 0.0，所有 band = 0.0   # 完全扁平
```

结论：
- preamp 提供 **±20 dB** 的纯净增益，覆盖全库 −31.2 ~ −7.6 的需求（最大需 +20 dB / −3.6 dB）。
- band 默认全 0 → **只改 preamp 不改变音色**，是纯增益。
- 音量滑杆保持用户专属，两者不打架。

### D2：测量只用 `ffmpeg ebur128`，且必须加 `-vn`

实测命令与真实输出（对 `王菲-EYES ON ME-英语-动画.mp4`）：

```
ffmpeg -hide_banner -nostdin -i <path> -vn -map 0:a:<n> -af ebur128=peak=true -f null -
```

```
Integrated loudness:
    I:          -9.7 LUFS
    Threshold: -20.2 LUFS
  Loudness range:
    LRA:         9.8 LU
  True peak:
    Peak:        2.7 dBFS
```

解析要点：从 stderr 取 **最后一个** `Integrated loudness:` 块里的 `I:`，以及 `True peak:` 块里的 `Peak:`。正则建议：

```python
_I_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+\.\d+)\s*LUFS")
_PEAK_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?\d+\.\d+)\s*dBFS")
```

`-vn` 的影响实测（同一批文件，NAS 冷缓存 → 热缓存）：**44× ~ 300× 实时，平均 98×**。不加 `-vn` 会把视频也解一遍，白白慢一个数量级。

### D3：`stream_index` 用「音轨序号（0 基）」而不是 ffprobe 的 stream index

实测 `ffprobe -select_streams a -show_entries stream=index` 对自建双音轨文件返回：

```
1
2
```

**不是 0/1**（与 libvlc 的音轨 ID 1/2 一致）。因此：

- 存 DB 的字段叫 `track_pos`（0 = 第一条音轨 = 原唱，1 = 第二条 = 伴奏），
- 播放时用 `player._audio_track_ids()[self._audio_track_index]` 的**位置**去查，
- 不要存 ffprobe 的原始 index，两边对不上会错位。

### D4：目标响度 −11.25 LUFS，但必须可配置

库内中位数即 −11.25。设为 `loudness_target` 配置项（范围 −30 ~ −5），默认 −11.25，留出按听感微调的空间。

### D5：增益必须做削波保护，且限制最大提升

```python
gain = target_lufs - lufs                    # 基础增益
if peak_db is not None:
    gain = min(gain, CEILING_DB - peak_db)   # CEILING_DB = -1.0，留 1 dB 余量
gain = max(MAX_ATTENUATE_DB, min(MAX_BOOST_DB, gain))  # 见下
```

- `CEILING_DB = -1.0`：已削波的源（例如 EYES ON ME 实测 `Peak: 2.7 dBFS`）只能被**衰减**，不能提升。
- `MAX_ATTENUATE_DB = -20.0`（preamp 硬下限）
- `MAX_BOOST_DB = +12.0`：**不是** 20。理由：提升 >12 dB 会把源里的底噪（老录像带源的嘶声、伴奏分离残留）一起放大，听感上「变响了但变脏了」。宁小勿噪，超出部分不补，并在日志里记一条 warning。

---

## 4. 架构与文件改动清单

| 文件 | 改动 |
|---|---|
| `ezkaraoke/loudness.py` | **新建**。测量逻辑（纯函数 + 并发 worker + CLI `__main__` 入口） |
| `ezkaraoke/database.py` | 新表 + 迁移 + CRUD（`get_loudness` / `put_loudness` / `loudness_pending` / `loudness_stats` / `prune_loudness`） |
| `ezkaraoke/player.py` | 新增 `_apply_loudness_gain()`，在 `_start_vlc` / `_swap_to` / `_sync_audio_track` / `toggle_audio_track` 之后调用 |
| `ezkaraoke/select_window.py` | 新菜单项 + 小面板（开始/停止/重测/开关）+ 进度复用 |
| `ezkaraoke/config.py` | 3 个新字段 + 校验 |
| `ezkaraoke/i18n.py` | 新增英文词条（中文是源语言，键就是中文串） |
| `tests/test_loudness.py` | **新建**。见 §9 |
| `tests/test_database.py` | 补迁移与 CRUD 用例 |

---

## 5. 数据库设计

### 5.1 新表（加进 `database.py` 的 `SCHEMA`）

```sql
CREATE TABLE IF NOT EXISTS loudness (
    path         TEXT    NOT NULL,
    track_pos    INTEGER NOT NULL DEFAULT 0,
    lufs         REAL,                 -- Integrated loudness；失败时为 NULL
    peak_db      REAL,                 -- True peak dBFS；解析不到时为 NULL
    duration     REAL,                 -- 次要，便于统计
    size         INTEGER,              -- 测量时的文件大小快照
    mtime        REAL,                 -- 测量时的文件 mtime 快照
    ok           INTEGER NOT NULL DEFAULT 1,   -- 0 = 测量失败（避免每次重试坏文件）
    measured_at  REAL    NOT NULL,
    tool         TEXT    NOT NULL DEFAULT 'ebur128',
    PRIMARY KEY (path, track_pos)
);
CREATE INDEX IF NOT EXISTS idx_loudness_path ON loudness(path);
```

### 5.2 失效与清理规则

- **失效判定**（模仿 `pitchshift.py` 缓存 key 含 `(path, size, mtime)` 的既有做法）：
  一个文件需要重测，当且仅当 `loudness` 里没有它的记录，或 `size`/`mtime` 与当前不一致。
- **`rebuild()` 里**加一行清理（与 `favorites` 的处理方式保持一致）：
  ```sql
  DELETE FROM loudness WHERE path NOT IN (SELECT path FROM songs)
  ```
- **`delete_song()` 里**同样清理该 path 的 loudness 行。

### 5.3 需要的方法（签名建议）

```python
def get_loudness(self, path: str) -> list[LoudnessRow]        # 按 track_pos 升序
def put_loudness(self, rows: list[LoudnessRow]) -> None       # INSERT OR REPLACE，批量
def mark_loudness_failed(self, path: str, track_pos: int) -> None   # ok=0
def loudness_pending(self) -> list[tuple[str, int, float, int]]
    """返回 [(path, size, mtime, size), ...]，即需要测量的文件（含所有音轨位置的占位）。
    只读 songs 表 + 已知 loudness 快照，不做文件系统调用。"""
def loudness_stats(self) -> dict    # count / ok / failed / min / median / max
def clear_loudness(self) -> None    # 「重新测量全部」
def prune_loudness(self) -> int     # 清掉已不在 songs 里的行，返回删除数
```

> 注意：`songs.size` 已经存在（`SizeBackfillWorker` 会回填），但**没有 mtime 列**。
> 实现时在 `loudness.py` 里对 pending 的文件调用 `os.stat` 取 `(size, mtime)` 后再比对，
> 避免为了 mtime 去改 `songs` 表结构（改动面更大）。

---

## 6. 测量 worker（`ezkaraoke/loudness.py`）

### 6.1 纯函数层（方便单测）

```python
@dataclass(frozen=True)
class LoudnessResult:
    lufs: float
    peak_db: float | None
    duration: float | None

def build_cmd(path: str, track_pos: int) -> list[str]:
    """ffmpeg 参数，-vn 跳过视频解码，-map 选中第 track_pos 条音轨。"""

def parse_ebur128(stderr: str) -> LoudnessResult | None:
    """解析 Summary 块；拿不到 I 就返回 None。"""

def measure_file(path: str, track_pos: int, timeout: float = 120.0) -> LoudnessResult | None:
    """同步测量一个音轨。ffmpeg 缺失 / 超时 / 解析失败 → None。"""

def audio_track_count(path: str) -> int:
    """ffprobe -select_streams a -show_entries stream=index；失败时返回 1。"""

def compute_gain(lufs: float, peak_db: float | None, target: float,
                 max_boost: float = 12.0, ceiling: float = -1.0) -> float:
    """§3 D5 的公式，夹在 [-20, max_boost]。纯函数，必测。"""
```

### 6.2 worker 层（严格模仿现有 `SizeBackfillWorker` 的形态）

```python
class LoudnessWorker(threading.Thread):
    """daemon 线程，绝不阻塞窗口关闭 / 进程退出。"""
```

必须遵守的既有约定（照着 `scanner.py` 的 `SizeBackfillWorker` / `ScanWorker` 抄）：

1. `threading.Thread(daemon=True)`；提供 `stop()` / `isRunning()` / `wait(ms)`（QThread 兼容命名）。
2. **自己开一个独立的 sqlite 连接**，绝不复用 GUI 线程的连接（现有代码注释明确要求）。
3. 信号用 `QObject` 上的 `Signal`，字段包括：`progress = Signal(int, int)`（done, total）、`finished = Signal()`、`error = Signal(str)`。信号从 worker 线程 emit，Qt 会自动排队到 GUI 线程。
4. 支持中途 `stop()`：每处理完一个文件检查一次 `self._stop.is_set()`。
5. 每完成 `_FLUSH_EVERY = 200` 个就 `commit()` 一次，保证断点续测真的能续。

### 6.3 并发策略

- 用 `concurrent.futures.ThreadPoolExecutor(max_workers=N)`（ffmpeg 是子进程，GIL 不阻塞）。
- `N` 默认 `loudness_workers = 8`（本机 24 核；**不要开满**，否则 8 并行测量会独占 NAS 带宽，影响正在播放的 VLC 读取）。
- 每个 future 完成后立刻写一条记录并 `commit()`（不要等全部结束）。
- 超时 `120s`/文件；超时视为失败（`ok=0`），记 log。
- ffmpeg 不存在（`shutil.which("ffmpeg") is None`）→ worker 立即 `finished` 并 `error.emit(tr("未找到 ffmpeg，无法测量响度"))`，**不抛异常**。

### 6.4 CLI 入口（不依赖 GUI，用于先在开发机跑全库）

```bash
python -m ezkaraoke.loudness --scan /mnt/NAS/Karaoke --workers 8 [--target -11.25] [--force]
```

- 复用同一套 `LoudnessWorker`（把信号接到一个打印进度的回调即可）。
- `--stats` 只打印统计不测量。
- 这个入口是**验收第 2 条**的执行方式，必须能独立跑通。

---

## 7. 播放时施加增益（`player.py`）

### 7.1 新增状态与开关

```python
self._loudness_enabled: bool = cfg.loudness_enabled
self._loudness_target: float = cfg.loudness_target
self._loudness_db: DatabaseLike | None = None    # 由 main/select_window 注入，避免 player 直接开库
```

> 注入方式：`PlayerController` 目前不持有 DB。建议新增 `def attach_database(self, db) -> None`，
> 在 `select_window.py` 创建 controller 的地方调用。测试里传一个内存库或 None（None 时功能静默关闭）。

### 7.2 实现

```python
def set_loudness_enabled(self, enabled: bool) -> None:
    self._loudness_enabled = bool(enabled)
    self._apply_loudness_gain()          # 立即生效（含关闭：把 preamp 归零）

def _apply_loudness_gain(self) -> None:
    """把当前歌曲/当前音轨对齐到目标响度。无 vlc / 无 DB / 未测过 → 直接归零。"""
    player = self._player
    if player is None:
        return
    gain = 0.0
    if self._loudness_enabled and self._db is not None and self._current_index >= 0:
        rows = self._db.get_loudness(self._queue[self._current_index].path)
        if rows:
            row = rows[min(self._audio_track_index, len(rows) - 1)]
            if row.lufs is not None:
                gain = compute_gain(row.lufs, row.peak_db, self._loudness_target)
    try:
        eq = vlc.AudioEqualizer()
        eq.set_preamp(gain)
        player.audio_set_equalizer(eq)
    except Exception:  # noqa: BLE001 - 响度对齐失败不能影响播放
        pass
```

### 7.3 调用点（一个都不能漏，漏了就会出现「切轨后音量跳变」）

| 位置 | 为什么 |
|---|---|
| `_start_vlc()`，紧跟在 `QTimer.singleShot(0, lambda: self._sync_audio_track(0))` 之后 | 新歌开始播放 |
| `_sync_audio_track()` 成功结束处（`self.audio_track_changed.emit(preferred)` 之前） | **libvlc 在 set_media 后会重置滤镜/EQ 状态**，必须在这里重设；同时这里才知道最终音轨序号 |
| `toggle_audio_track()` 成功切换之后 | 原唱↔伴奏响度不同（实测 −11.86 vs −11.98，将来可能差更多） |
| `_swap_to()` 里 `set_media` + `play()` 之后 | 变调变体是**另一个文件**（不同 path），缓存文件没有自己的测量记录 |
| `set_loudness_enabled()` / 修改 target 时 | 立即生效 |

### 7.4 变调变体的处理（重要陷阱）

`pitchshift.shifted_file_path()` 生成的是**不同路径**的新文件（cache 目录），DB 里不会有它的测量记录。
处理方式：**查不到就用源文件的测量值**（变调不改变响度）。

```python
def _loudness_rows_for_active(self) -> list[LoudnessRow]:
    """active_path 无记录时，回退到当前歌曲的原始 path。"""
```

不要给变调缓存文件建测量记录（缓存会被 `prune_cache()` 删除，会留下垃圾行）。

### 7.5 边界

- 播放中开关切换：只改 preamp，不需要重载媒体（`audio_set_equalizer` 立即生效）。
- `_player is None`（无 libvlc）：全部 no-op，与现有代码风格一致。
- 轨道数 > 2 的文件（现有库里有双语种多轨）：`min(self._audio_track_index, len(rows)-1)` 兜底。

---

## 8. 配置与 UI

### 8.1 `config.py` 新增字段（跟随现有手写校验风格，非法值回落默认）

```python
loudness_enabled: bool = True
loudness_target: float = -11.25      # 允许 -30 .. -5
loudness_workers: int = 8            # 允许 0 .. 16；0 = auto(cpu/2)
```

### 8.2 UI（`select_window.py`）

- 在状态栏区域（`_btn_rescan` 附近）加一个按钮/菜单项：**「响度对齐」**。
- 点击展开一个小面板（`QMenu` 或轻量 `QDialog`，与项目现有风格一致即可），内容：

  - 一行状态：`已测 15,237 / 15,237（失败 12）`、`目标 -11.25 LUFS`
  - 按钮：**开始测量** / **停止** / **重新测量全部**（二次确认）/ **开关：启用/停用**
  - 一行微调：目标响度 `QDoubleSpinBox`（-30 ~ -5，step 0.25）

- 进度复用现有的三件套：
  - `_set_progress_count(tr("正在测量响度"), done, total)`
  - `_set_progress_busy(...)` / `_set_progress_idle()`
- 必须在 `closeEvent` 的收尾清单里把 `_loudness_worker` 一起 `stop()` + `wait()`（照 `_scan_worker`、`_size_worker` 的写法，见 `select_window.py` 约 1249 行）。

### 8.3 可选（做了更好）

播放器窗口状态栏 tooltip 显示当前增益，例如：`-11.86 LUFS → +0.6 dB`。方便排查「这首歌为什么还是小声」。

### 8.4 i18n

在 `i18n._EN` 里补齐所有新键（中文即键）：

```python
"响度对齐": "Loudness",
"正在测量响度": "Measuring loudness",
"未找到 ffmpeg，无法测量响度": "ffmpeg not found; cannot measure loudness",
"重新测量全部": "Re-measure all",
"目标响度": "Target loudness",
...
```

`tests/test_i18n.py` 里若有「所有键都有英文」的检查，新键必须补齐，否则测试会红。

---

## 9. 测试要求（`tests/test_loudness.py`）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 用 ffmpeg 现场合成两个正弦波文件（如 −20 LUFS / −6 LUFS，各 5 秒，`anullsrc`/`sine` + `volume`） | `measure_file()` 返回值与设定值误差 **≤ 0.5 LU** |
| 2 | `compute_gain()` 纯函数 | ①正常提升；②`peak_db=2.7`（已削波）时只能衰减；③`lufs=-31.2` 时被夹在 `max_boost`；④`lufs=-7.6` 时为负增益 |
| 3 | DB 迁移 | 用**旧 schema**（只有 songs/artists/favorites）建库 → `SongDatabase(path)` 打开 → 断言 `loudness` 表存在且 `PRAGMA table_info` 有两列主键 |
| 4 | 幂等 | worker 跑两遍，第二遍 `loudness_pending()` 为空 |
| 5 | 失效重测 | 把文件 `os.utime` 改成新 mtime → `loudness_pending()` 重新包含该路径 |
| 6 | 无 ffmpeg 降级 | `monkeypatch` `shutil.which("ffmpeg") -> None` → worker 立刻结束、`error` 信号有值、不抛异常 |
| 7 | 多音轨 | 合成双音轨文件（两条音轨响度差 ≥ 6 dB）→ 断言两条记录 `track_pos` 0/1 且数值不同 |
| 8 | 不修改文件 | 测前后对文件算 `hashlib.md5`，断言一致（**这条是硬性安全断言**） |
| 9 | `delete_song()` / `rebuild()` | 删除后 loudness 行同步消失 |
| 10 | UI smoke | 沿用 `test_ui_smoke.py` 的模式，断言新按钮存在、点击后 worker 被创建、`closeEvent` 能收干净 |

运行：`uv run pytest`（项目 `pyproject.toml` 已配 `testpaths = ["tests"]`，`pytest>=8`）。

---

## 10. 验收标准（人工 + 可自动化）

1. **进度可见**：打开软件后触发测量，状态栏显示 `正在测量响度 x/y`；过程中关窗口不崩、不留僵尸 ffmpeg 进程（`pgrep -f ffmpeg` 为空）。
2. **覆盖率**：全库测量完成后，`SELECT COUNT(*) FROM loudness WHERE ok=1` 覆盖歌曲表的全部音轨；失败项有 `ok=0` 标记且不会每次启动重试。**先用 CLI 跑一遍即可验收这一条**：

   ```bash
   python -m ezkaraoke.loudness --scan /mnt/NAS/Karaoke --workers 8
   python -m ezkaraoke.loudness --stats
   ```

   预期耗时：本机 24 核、NAS 热缓存下 **8 并行 ≈ 1.7 小时**（实测平均 98× 实时）。
3. **听感**（真人验收，必须做）：挑 5 首原本响度差 >10 dB 的歌连续播放，**切换时不需要再动音量滑杆**，主观响度差异 ≤ 2 dB。
4. **开关有效**：关闭开关后，行为与改造前**完全一致**（同一首歌同一音量）。
5. **自动维护**：往库里加一首新歌 → 重新扫描后自动进入待测队列；删歌 → loudness 行同步消失。
6. **零改动证明**：抽 5 个文件测前后比对 md5 + `ls -l` 大小/mtime，全部一致。

---

## 11. 风险与已知限制（写进 README 的「已知限制」一节）

1. **提升 >12 dB 会把底噪一起放大**：库里有 −31 LUFS 的老录像带源，这类文件对齐后「响度够了但嘶声明显」。这是物理限制，不是 bug。策略是 `MAX_BOOST_DB = 12`，超出部分不补，宁可偏小。
2. **文件内部的动态范围仍不一致**：库内 LRA 最高 19.8 LU，静态增益无法对齐「一首歌内部」的起伏。本方案不解决这个，也不应解决（那需要动态压缩，会破坏音乐）。
3. **只在 ezkaraoke 里生效**：这是本方案的核心权衡——代价是不改文件，收益是零风险、秒级生效、可随时反悔。若哪天需要「在手机/其他播放器上也一致」，那才需要走离线归一化路线（已有现成流程，但那次要单独评估空间与时间预算）。
4. **测量值随源文件变化而失效**：靠 `size`/`mtime` 快照判定，若用户用别的手段替换了文件内容但保持大小与 mtime 不变，会用到旧值——可接受。
5. **VLC 4 升级注意**：`±20 dB` 是 libvlc 3.x 的 preamp 约定。将来升级 libvlc 4 时，需重新确认 `libvlc_audio_equalizer_get_preamp` 的边界并更新夹取常量（`MAX_ATTENUATE_DB`），本项目已在 `README` 的「VLC 版本」一节记录当前实测版本 `3.0.23 Vetinari`。
6. **NAS 带宽**：8 并行测量会占满千兆网口。测量期间若同时在唱歌，建议把 `loudness_workers` 调到 2~3（UI 面板里可以直接改）。

---

## 12. 交付物清单

- [ ] `ezkaraoke/loudness.py`（含 `python -m` CLI）
- [ ] `database.py` 的表 + 迁移 + 方法
- [ ] `player.py` 的增益施加与 5 个调用点
- [ ] `config.py` 三字段 + 校验
- [ ] `select_window.py` 面板 + 收尾清理
- [ ] `i18n.py` 英文词条
- [ ] `tests/test_loudness.py`（§9 全 10 条）+ `test_database.py` 补用例
- [ ] README「响度对齐」与「已知限制」两节

---

## 附录 A：实测数据（用于比对验收）

| 项目 | 数值 |
|---|---|
| 库文件数 / 大小 | 15,237 / 1.1 TB |
| 响度中位数（抽样 16） | −11.25 LUFS |
| 响度全距 | −31.2 ~ −7.6 LUFS |
| 测量速度（`-vn` + `ebur128`） | 44× ~ 300× 实时，平均 **98×** |
| 全库测量预估 | 单线程 ~13.8 h；**8 并行 ≈ 1.7 h** |
| libvlc 版本 | 3.0.23 Vetinari |
| preamp 实测范围 | **±20.0 dB**（超限自动夹取） |
| 新建 EQ 默认 | preamp 0.0，所有 band 0.0（纯增益，不改音色） |
| 双音轨文件的 ffprobe stream index | **1, 2**（不是 0, 1） |
| 已手工归一化的样本 | LOST STARS（Adam Levine）原唱 −11.59 / 伴奏 −11.83；Keira Knightley 版 −11.86 / −11.98 |

## 附录 B：可直接复制的测量命令

```bash
# 测量某个文件第一条音轨（人眼可读）
ffmpeg -hide_banner -nostdin -i "<file>" -vn -map 0:a:0 -af ebur128=peak=true -f null -

# 探测该文件有几条音轨
ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "<file>"

# 验证「测量不写文件」：测前后 md5 一致
md5sum "<file>"   # 测量前后各跑一次比对
```
