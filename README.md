# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Scoped to **`01.图像视频重命名工具.py`** only. The parent directory (`Z:\Programs\py脚本\`) holds many unrelated standalone scripts; do not assume anything you find here applies to them. Other `*.CLAUDE.md` files in this folder are owned by their respective scripts.
>
> 本文档**仅适用于** `01.图像视频重命名工具.py`。父目录下的其他脚本各自独立,不要把这里的约定套用到它们身上。

---

## Run / Invoke 运行方式

GUI mode (default):

```bash
python "01.图像视频重命名工具.py"
```

CLI single-folder mode (entry point at the bottom of the file, used by drag-and-drop / shell integrations):

```bash
python "01.图像视频重命名工具.py" "<folder_path>"
```

The CLI path uses a hardcoded `custom_prefix='nb01.xyz'` and runs `generate_preview` + `execute_rename` non-interactively (lines 1818-1825). It does **not** apply any user config from `rename_config.json` — keep that in mind before reusing it for batch automation. The CLI always runs **non-recursively** (`include_subdirs` defaults to `False`).

There is no test suite, build step, linter config, or `requirements.txt`. Dependencies are stdlib only (`tkinter`, `pathlib`, `shutil`, `queue`). On Windows, `ctypes.windll.shcore.SetProcessDpiAwareness(1)` is called for HiDPI; the `try/except: pass` around it is intentional for non-Windows fallback.

不要新增依赖。本脚本刻意只用标准库,方便单文件分发。

---

## Architecture 架构

Three classes in one file, clean separation:

- **`ConfigManager`** — wraps `rename_config.json` (next to the script). Holds UI prefs *and* window geometry. Saved on `WM_DELETE_WINDOW`.
- **`FileRenamer`** — pure logic, no Tk references. Produces a `preview_list` first, then `execute_rename` consumes it. **All UI-blocking work lives here** and must stay Tk-free so it can run in worker threads.
- **`RenamerGUI`** — Tkinter front-end. Owns threading, log queue, progress bar, and all `messagebox` calls.

### The two-phase pipeline 两阶段流水线

The UI **never** renames in one shot. The flow is always:

1. **Scan** (`scan_folders` → `FileRenamer.scan_subdirectories`) — enumerate first-level subdirs of each user-added folder, count image/video files in each, present a checkable list. The root directory itself is included as a synthetic `[根目录]` entry **only in flat mode** (when `包含子文件夹` is off); with the option on, the **parent/root entry is omitted entirely** — the user never sees the base folder as a batch target. When the option is on, the listed counts are **recursive** (they include files in nested folders), though the list still only shows first-level entries.
2. **Preview** (`generate_preview` → `FileRenamer.generate_preview`) — for the *checked* subdirs only, build `preview_list` entries with status `rename` / `skip_date` / `same`. Nothing is touched on disk. When `包含子文件夹` is on, each checked folder is processed **recursively**: every directory inside the tree gets its **own independent counter** (each folder's sequence restarts at 1, exactly like the legacy flat mode), conflict detection happens per real directory, and the folder-name prefix uses the checked folder's basename for the whole tree.
3. **Execute** (`start_rename` → `FileRenamer.execute_rename`) — iterates only `status == 'rename'` items.

If you add a new naming option, you must thread it through `get_prefix_config()` → `prefix_config` dict → `generate_new_filename()`. Don't shortcut into the renamer with kwargs — the dict shape is the contract. Recursion is threaded the same way: `include_subdirs` flows from the GUI checkbox into `scan_subdirectories(..., include_subdirs)` and `generate_preview(..., include_subdirs=...)` as a plain parameter (not via config dict).

### Undo & 模糊匹配 (the non-obvious part)

After every successful `execute_rename`, a JSON file `图像视频重命名记录_YYYYMMDD_HHMMSS.json` is dropped next to the script. On startup, `auto_load_latest_record` picks the **most recent** by mtime and loads it. This is why undo works across program restarts.

`resolve_actual_path` (lines 149-210) is the load-bearing function for "the user renamed the parent folder after running rename, e.g. added a `(71P+3V_132.3M)` suffix". It walks the recorded path component by component; when a segment doesn't exist, it strips standardized size markers via `clean_folder_markers` and looks for a sibling whose cleaned name matches. **Results are cached in `_path_resolve_cache`**, cleared at the start of each `undo_rename`. If you change the folder-marker convention, update the regex at line 145.

`undo_rename` is **partial-recovery aware**: only failed records survive in `self.rename_records`. `update_record_file` then either deletes the JSON (all recovered) or rewrites it with just the leftovers. Don't break this contract — users rely on retrying undo after fixing path issues.

### Threading & UI responsiveness 线程模型

Every long-running op runs in a `threading.Thread(daemon=True)`:

- Worker calls back to the UI **only** via `self.root.after(0, lambda: ...)` for state changes.
- Logs go through `log_queue` (a stdlib `queue.Queue`); UI flushes the whole queue every 200 ms via `_schedule_log_flush`. **Don't call `log_text.insert` directly from anywhere** — always go through `self.log()`.
- `count_callback` updates the determinate progress bar. It only sets the value; `maximum` is set once when the operation starts (lines 1446, 1719). Re-configuring `maximum` per tick triggers extra widget redraws and was the source of a felt-jankiness regression — keep it that way.
- `execute_rename` / `undo_rename` throttle log + progress callbacks to ~50 reports total via `report_step = max(1, total // 50)`. Per-file logging will lag the UI on 1000+ files.

### File-type & counter logic 计数器

Counters are per-directory and split four ways: `image / video / gif / other`. `.gif` is intentionally pulled out of `video` so it gets the `@gif` label instead of `@Video`. Sort key in `generate_preview` puts files with `thumb` in the name *last* so the main image gets the lower index. The `@thumb` suffix is preserved in the new name when the original contained it (case-insensitive). Don't "simplify" any of these — they encode user-facing conventions. With `包含子文件夹` on, **every directory in the tree keeps its own counter** (each folder restarts its sequence at 1 — same convention as flat mode, so files in different folders may share generated names, which is fine since they live in different directories); the sort is grouped by relative directory first, then thumb-last, then name.

Filename-conflict resolution: if the generated name collides with `existing_files` (which is a snapshot of the dir at scan time **plus** names already produced this batch — the discard/add at lines 462-464 is what makes this correct), the counter is bumped until unique. Same-batch collisions are why that回写 (write-back) is required.

---

## Config & runtime artifacts 运行时文件

All in the script's directory:

| File | Purpose | Safe to delete? |
|---|---|---|
| `rename_config.json` | UI prefs + last folder + window geometry | Yes (resets to defaults) |
| `图像视频重命名记录_*.json` | Undo records, one per rename batch | Yes if you don't need to undo that batch |

`auto_load_latest_record` only loads **one** (the newest by mtime). Older records keep accumulating until manually cleaned — the script does not garbage-collect them.

---

## When editing 编辑须知

- Anything that takes more than ~200 ms (scan, preview, rename, undo) **must** stay off the Tk main thread. Adding a synchronous call in a UI handler will reintroduce window freezing on large batches.
- Log levels are `info` / `success` / `warning` / `error` — stick to these so the prefix mapping in `log()` stays accurate.
- Tk variables (`*_var`) are the source of truth for UI state, not Python attributes. Read them via `.get()` inside callbacks, not at startup.
- `shutil.move` is used instead of `os.rename` because users routinely rename across drives (e.g., `Y:\` → `D:\`); don't switch back.
- Windows-illegal characters are stripped from folder names via the regex on line 301 — both `？` (full-width) and `?` (half-width) matter.
- The script title in the window says `v4.7` (line 692) and the docstring at the top says `v4.7`. Bump both together when versioning, and add a row to 变更日志 below.
- `_count_files_in_dir` / `_collect_files_flat` / `_collect_files_recursive` all use `entry.is_file(follow_symlinks=False)` / `is_dir(follow_symlinks=False)`. Keep that: the recursive walk must never follow symlinks or junction points, or it can loop forever.

---

## What this script is *not* 边界

Not a general file mover, not a deduper. Recursion past the first subdir level is **opt-in** via the `包含子文件夹` checkbox: when unchecked, only the files directly inside the checked subfolders are processed, and the `[根目录]` entry is offered (legacy behavior); when checked, each checked folder is processed recursively down its whole tree — the parent/root entry is **not** listed, and every folder in the tree keeps its own independent sequence. If the user ever has overlapping selected folders (e.g. two base folders where one nests inside the other), the nested one is silently dropped (logged as a warning) so files are never processed twice. Don't bolt on content-based dedup; that belongs in a sibling script.

---

## 变更日志 / Changelog

### v4.7 (2026-08-25)

**新增：包含子文件夹选项（递归重命名）**

- 重命名设置 → 选项 中新增勾选框 `包含子文件夹(递归处理所选文件夹下的全部子文件夹)`，状态保存到 `rename_config.json`。
- 勾选后，`生成预览` 对每个所选文件夹**递归**处理其全部子文件夹：
  - **序列各文件夹独立**：计数器按文件实际所在目录各自从 1 开始（图片/视频/gif 各自计数），与既有平铺模式"每个文件夹一个序列"的约定完全一致，不再跨文件夹连续编号；
  - 文件名冲突检测按文件**实际所在目录**进行（新增 `_snapshot_existing_files` 按目录缓存）；
  - 文件夹名前缀取**所选文件夹**的名字，整棵子树共用；
  - 排序按相对目录分组 → thumb 文件排后 → 名称，保证顺序稳定且与既有 thumb 约定一致。
- 勾选后，`扫描文件夹` 的列表**不再包含母目录/根目录**（如 `[根目录] Y:\1` 这类条目被移除，只有平铺模式保留它），列表仍只显示一级子文件夹，统计数量递归包含嵌套文件夹，计数标签追加"（含子文件夹）"提示。
- **防重复**：多基目录下若所选目录存在父子包含关系，只处理最上层，被覆盖的子目录会在预览日志中提示"已跳过"（`_filter_nested_directories`）。
- **防呆**：切换该选项后，旧的扫描统计与预览结果自动失效（禁用"生成预览/开始重命名"按钮并提示重新扫描），避免执行过期预览。
- 撤销功能不受影响：记录保存完整路径，跨层级、跨文件夹标注后缀的模糊匹配（`resolve_actual_path`）按路径逐段解析，天然支持嵌套目录。
- 实现：`FileRenamer` 新增 `_is_target_file` / `_collect_files_flat` / `_collect_files_recursive`（显式栈 + `follow_symlinks=False`，不跟随符号链接/联接点，避免循环）/ `_snapshot_existing_files` / `_is_within` / `_filter_nested_directories`；`generate_preview` 与 `scan_subdirectories` / `_count_files_in_dir` 增加 `include_subdirs` 参数（默认 `False`，CLI 模式保持不递归，旧调用签名完全兼容）。

**冲突检查结论**

- 递归模式与既有"跳过含日期文件 / thumb 后缀 / gif 独立计数 / 类型标签"约定无冲突，已通过临时冒烟测试验证：扫描统计、平铺预览（行为不变）、递归预览（嵌套文件全部纳入、各文件夹独立序列、文件夹内文件名唯一）、母目录不列入、多基目录重叠去重、递归重命名→撤销端到端恢复、二次预览结果一致。
- 已知边界：递归遍历遇到无权限目录会静默跳过该子树（统计可能偏少，预览日志可见"目录不存在"类提示）；极端深目录树使用显式栈遍历，不受 Python 递归深度限制。

