"""
图像视频重命名工具 v4.7
功能：批量重命名图片和视频文件，支持预览、撤销、配置保存
新增：撤销记录持久化、自动加载历史记录、文件夹名模糊匹配
新增：包含子文件夹选项，勾选后递归处理所选文件夹下的全部子文件夹（各文件夹独立编号）
"""


import os
import sys
import re
import json
import shutil
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
import threading
from pathlib import Path


# ==================== 配置管理 ====================
class ConfigManager:
    """配置管理类 - 保存和加载用户设置"""
    
    def __init__(self, config_file="rename_config.json"):
        self.config_file = config_file
        self.config = self.load_config()
    
    def load_config(self):
        """加载配置文件"""
        default_config = {
            "default_folder": r"Y:\1",
            "prefix": "nb01.xyz",
            "file_type": "全部",
            "separator": "_",
            "number_format": "04d",
            "use_folder_name": False,
            "use_original_name": False,
            "use_file_date": False,
            "use_current_date": False,
            "keep_date_in_name": False,
            "add_type_label": True,
            "skip_date_files": True,
            "include_subdirs": False,
            "original_name_max_len": 0,   # 0 = 不截断原文件名（仅在文件名过长时自动裁剪）
            "window_width": 1100,
            "window_height": 800,
            "window_x": None,
            "window_y": None
        }
        
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    default_config.update(saved_config)
                # 旧版本把 10 硬编码写进配置文件，该值并非用户设置，迁移为“不限制”
                if default_config.get("original_name_max_len") == 10:
                    default_config["original_name_max_len"] = 0
            except Exception as e:
                print(f"加载配置失败：{e}")
        
        return default_config
    
    def save_config(self, config_dict):
        """保存配置文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_dict, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存配置失败：{e}")
            return False
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value


# ==================== 重命名记录 ====================
class RenameRecord:
    """重命名记录类 - 用于撤销"""
    
    def __init__(self, old_path, new_path, timestamp):
        self.old_path = old_path
        self.new_path = new_path
        self.timestamp = timestamp
    
    def to_dict(self):
        return {
            'old_path': self.old_path,
            'new_path': self.new_path,
            'timestamp': self.timestamp
        }
    
    @staticmethod
    def from_dict(data):
        return RenameRecord(data['old_path'], data['new_path'], data['timestamp'])


# ==================== 文件重命名核心 ====================
class FileRenamer:
    """文件重命名核心类"""

    # 单个文件名（含扩展名）的安全上限；NTFS 上限 255，留出余量给冲突序号等
    MAX_FILENAME_LEN = 200
    
    def __init__(self):
        self.total_renamed = 0
        self.total_skipped = 0
        self.error_count = 0
        self.error_details = []
        self.stop_flag = False
        self.rename_records = []
        self.preview_list = []
        self.current_record_file = None
        self._path_resolve_cache = {}
        
        self.image_extensions = [
            # 传统 / 通用
            '.jpg', '.jpeg', '.jpe', '.jfif', '.insp',
            '.png', '.bmp', '.tiff', '.tif', '.gif',
            '.webp', '.svg', '.psd',

            # 高效 / 现代压缩格式
            '.heic', '.heif', '.hif', '.avif', '.jxl',

            # RAW 格式
            '.cr2', '.cr3', '.crw',          # Canon
            '.nef', '.nrw',                  # Nikon
            '.arw', '.srf', '.sr2',          # Sony
            '.raf',                          # Fujifilm
            '.orf',                          # Olympus
            '.rw2', '.raw',                  # Panasonic
            '.dng',                          # Adobe / 通用 RAW
            '.pef',                          # Pentax
            '.3fr',                          # Hasselblad
            '.x3f',                          # Sigma
        ]
        
        self.video_extensions = [
            '.mp4', '.ts', '.mov', '.avi', '.mkv', '.flv', '.wmv', 
            '.webm', '.gif', '.mts', '.m4v'
        ]
        
        self.all_extensions = [ext.lower() for ext in self.image_extensions + self.video_extensions]
    
    def has_date_pattern(self, filename):
        """检查文件名是否包含日期格式 (YYYY-MM-DD)"""
        date_pattern = r'20\d{2}-\d{2}-\d{2}'
        return bool(re.search(date_pattern, filename))
    
    def get_file_type(self, filename):
        """判断文件类型"""
        ext = os.path.splitext(filename)[1].lower()
        if ext in self.image_extensions:
            return 'image'
        elif ext in self.video_extensions:
            return 'video'
        return 'other'
    
    @staticmethod
    def clean_folder_markers(folder_name):
        """
        清除文件夹名中的标注后缀
        支持格式: (71P_132.3M) (71P+3V_132.3M) (71P+3V+2Gif_132.3M) 等
        """
        pattern = r'\s*\((\d+P)?(\+?\d+V)?(\+?\d+Gif)?_?\d+\.?\d*[MG]\)\s*'
        cleaned = re.sub(pattern, '', folder_name).strip()
        return cleaned
    
    def resolve_actual_path(self, recorded_path):
        """
        解析实际磁盘路径：如果记录中的路径不存在，尝试模糊匹配
        返回: (实际路径, 是否经过模糊匹配)
        """
        if recorded_path in self._path_resolve_cache:
            return self._path_resolve_cache[recorded_path]

        if os.path.exists(recorded_path):
            result = (recorded_path, False)
            self._path_resolve_cache[recorded_path] = result
            return result

        parts = Path(recorded_path).parts
        current_path = parts[0]

        for i in range(1, len(parts)):
            next_path = os.path.join(current_path, parts[i])

            if os.path.exists(next_path):
                current_path = next_path
                continue

            if not os.path.exists(current_path):
                result = (recorded_path, False)
                self._path_resolve_cache[recorded_path] = result
                return result

            recorded_name_clean = self.clean_folder_markers(parts[i])

            matched = False
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        if i == len(parts) - 1:
                            if entry.name == parts[i]:
                                current_path = entry.path
                                matched = True
                                break
                        else:
                            if entry.is_dir(follow_symlinks=False):
                                entry_clean = self.clean_folder_markers(entry.name)
                                if entry_clean == recorded_name_clean:
                                    current_path = entry.path
                                    matched = True
                                    break
            except PermissionError:
                result = (recorded_path, False)
                self._path_resolve_cache[recorded_path] = result
                return result

            if not matched:
                result = (recorded_path, False)
                self._path_resolve_cache[recorded_path] = result
                return result

        if os.path.exists(current_path):
            result = (current_path, current_path != recorded_path)
        else:
            result = (recorded_path, False)
        self._path_resolve_cache[recorded_path] = result
        return result
    
    def scan_subdirectories(self, directory, file_type='all', include_subdirs=False):
        """
        扫描目录下的一级子文件夹，返回子文件夹信息列表
        每项: {'path': 路径, 'name': 名称, 'image_count': 图片数, 'video_count': 视频数}
        include_subdirs=True 时，统计数量递归包含各子文件夹下的文件
        """
        result = []
        
        if not os.path.exists(directory):
            return result
        
        # 检查根目录自身是否有目标文件（包含子文件夹模式下不列出母目录/根目录）
        if not include_subdirs:
            root_files = self._count_files_in_dir(directory, file_type, recursive=False)
            if root_files['image_count'] > 0 or root_files['video_count'] > 0:
                result.append({
                    'path': directory,
                    'name': f"[根目录] {os.path.basename(directory)}",
                    'image_count': root_files['image_count'],
                    'video_count': root_files['video_count'],
                    'is_root': True
                })
        
        # 扫描一级子文件夹
        try:
            with os.scandir(directory) as it:
                entries = sorted(
                    [e for e in it if e.is_dir(follow_symlinks=False)],
                    key=lambda e: e.name
                )
            for entry in entries:
                counts = self._count_files_in_dir(entry.path, file_type, recursive=include_subdirs)
                if counts['image_count'] > 0 or counts['video_count'] > 0:
                    result.append({
                        'path': entry.path,
                        'name': entry.name,
                        'image_count': counts['image_count'],
                        'video_count': counts['video_count'],
                        'is_root': False
                    })
        except PermissionError:
            pass

        return result

    def _count_files_in_dir(self, directory, file_type='all', recursive=False):
        """统计目录中的图片和视频数量；recursive=True 时递归统计全部子文件夹"""
        if recursive:
            entries = self._collect_files_recursive(directory, file_type)
        else:
            entries = self._collect_files_flat(directory, file_type)

        image_count = 0
        video_count = 0
        for _, _, filename in entries:
            f_type = self.get_file_type(filename)
            if f_type == 'image':
                image_count += 1
            elif f_type in ['video', 'other']:
                video_count += 1

        return {'image_count': image_count, 'video_count': video_count}

    def _is_target_file(self, filename, file_type='all'):
        """判断文件是否属于目标类型（扩展名 + 类型过滤）"""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in self.all_extensions:
            return False
        f_type = self.get_file_type(filename)
        if file_type == 'image' and f_type != 'image':
            return False
        elif file_type == 'video' and f_type not in ['video', 'other']:
            return False
        return True

    def _collect_files_flat(self, directory, file_type='all'):
        """收集目录本身（不递归）的目标文件，返回 [(dirpath, rel_dir, filename)]"""
        entries = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and self._is_target_file(entry.name, file_type):
                        entries.append((directory, '', entry.name))
        except PermissionError:
            pass
        return entries

    def _collect_files_recursive(self, directory, file_type='all'):
        """
        递归收集目录树下全部目标文件，返回 [(dirpath, rel_dir, filename)]
        使用显式栈 + follow_symlinks=False，不跟随符号链接/联接点，避免循环
        """
        entries = []
        stack = [directory]
        while stack:
            if self.stop_flag:
                break
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    subdirs = []
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(entry)
                        elif entry.is_file(follow_symlinks=False) and self._is_target_file(entry.name, file_type):
                            rel_dir = '' if current == directory else os.path.relpath(current, directory)
                            entries.append((current, rel_dir, entry.name))
                    subdirs.sort(key=lambda e: e.name)
                    stack.extend(subdirs)
            except PermissionError:
                continue
        return entries

    def _snapshot_existing_files(self, dirpath):
        """获取目录中现有文件名集合（用于冲突检测）"""
        try:
            with os.scandir(dirpath) as it:
                return {e.name for e in it}
        except PermissionError:
            return set()

    @staticmethod
    def _is_within(child, parent):
        """判断 child 是否位于 parent 目录之下（含自身；Windows 下不区分大小写）"""
        try:
            c = os.path.normcase(os.path.abspath(child))
            p = os.path.normcase(os.path.abspath(parent))
            return os.path.commonpath([c, p]) == p
        except ValueError:
            return False

    @staticmethod
    def _dedupe_directories(directories):
        """去掉重复的目录（Windows 下不区分大小写），保留首次出现的顺序"""
        seen = set()
        result = []
        for d in directories:
            key = os.path.normcase(os.path.abspath(d))
            if key in seen:
                continue
            seen.add(key)
            result.append(d)
        return result

    def _filter_nested_directories(self, directories, dropped_callback=None):
        """去掉被其他所选目录包含的子目录，避免递归模式下同一批文件被重复处理"""
        dirs = [os.path.abspath(d) for d in directories]
        result = []
        for i, d in enumerate(dirs):
            nested = False
            for j, other in enumerate(dirs):
                if i == j:
                    continue
                if self._is_within(d, other):
                    nested = True
                    break
            if nested:
                if dropped_callback:
                    dropped_callback(d)
            else:
                result.append(d)
        return result
    
    def generate_new_filename(self, filename, prefix_config, counter, folder_name=''):
        """根据配置生成新文件名"""
        extension = os.path.splitext(filename)[1]
        file_type = self.get_file_type(filename)
        name_without_ext = os.path.splitext(filename)[0]
        
        # 检测原文件名是否含 thumb（忽略大小写）
        is_thumb = 'thumb' in name_without_ext.lower()
        
        prefix_parts = []
        orig_index = None  # 原文件名在 prefix_parts 中的位置，过长时只裁剪这一段
        
        if prefix_config.get('use_custom_prefix', True) and prefix_config.get('custom_prefix', ''):
            prefix_parts.append(prefix_config['custom_prefix'])
        
        if prefix_config.get('use_folder_name', False) and folder_name:
            clean_folder = re.sub(r'[<>:"/\\|？\?*]', '_', folder_name)
            prefix_parts.append(clean_folder)
        
        if prefix_config.get('use_original_name', False):
            if prefix_config.get('keep_date_in_name', False) or not self.has_date_pattern(filename):
                # max_len <= 0 表示不限制长度（默认）；只有显式设置了正数才截断
                try:
                    max_len = int(prefix_config.get('original_name_max_len', 0) or 0)
                except (TypeError, ValueError):
                    max_len = 0
                orig_name = name_without_ext[:max_len] if max_len > 0 else name_without_ext
                orig_index = len(prefix_parts)
                prefix_parts.append(orig_name)
        
        if prefix_config.get('use_file_date', False):
            date_match = re.search(r'20\d{2}-\d{2}-\d{2}', filename)
            if date_match:
                prefix_parts.append(date_match.group())
        elif prefix_config.get('use_current_date', False):
            prefix_parts.append(datetime.now().strftime('%Y-%m-%d'))
        
        separator = prefix_config.get('separator', '_')
        number_format = prefix_config.get('number_format', '04d')
        
        type_label = ''
        if prefix_config.get('add_type_label', True):
            if file_type == 'image':
                type_label = ''
            elif extension.lower() == '.gif':
                type_label = '@gif'
            else:
                type_label = '@Video'
        
        # 构建 thumb 后缀
        thumb_suffix = '@thumb' if is_thumb else ''
        
        def build():
            base_prefix = (separator.join(prefix_parts) if prefix_parts
                           else prefix_config.get('custom_prefix', 'file'))
            return f'{base_prefix}{type_label}_{counter:{number_format}}{thumb_suffix}{extension}'
        
        new_filename = build()
        
        # 仅当整体文件名超出安全上限时，才裁剪“原文件名”这一段（只裁多出来的部分）
        if orig_index is not None and len(new_filename) > self.MAX_FILENAME_LEN:
            excess = len(new_filename) - self.MAX_FILENAME_LEN
            orig = prefix_parts[orig_index]
            keep = max(1, len(orig) - excess)
            prefix_parts[orig_index] = orig[:keep].rstrip() or orig[:keep]
            new_filename = build()
        
        return new_filename

    
    def generate_preview(self, directories, prefix_config, file_type='all',
                        enable_date_check=True, include_subdirs=False,
                        progress_callback=None):
        """
        生成重命名预览（不实际重命名）

        include_subdirs=True 时，对每个所选目录递归处理其全部子文件夹：
        - 计数器按文件实际所在目录各自独立（每个文件夹单独从 1 开始编号，不跨文件夹连续）
        - 冲突检测按文件实际所在目录进行
        - 文件夹名前缀取所选目录的名字，整棵子树共用
        - 若所选目录之间存在父子包含关系，只处理最上层目录，避免重复
        """
        self.preview_list = []
        self.total_skipped = 0
        
        image_count = 0
        video_count = 0

        # 先去掉重复目录（同一目录被选了两次会导致同一批文件被处理两次）
        directories = self._dedupe_directories(directories)

        # 递归模式下，若所选目录存在父子包含关系，只保留最上层目录，避免同一批文件被处理两次
        if include_subdirs and len(directories) > 1:
            directories = self._filter_nested_directories(
                directories,
                dropped_callback=(lambda d: progress_callback(
                    f"已跳过被上级目录覆盖的子目录：{d}", 'warning'
                )) if progress_callback else None
            )
        
        for directory in directories:
            if self.stop_flag:
                break
            
            if not os.path.exists(directory):
                if progress_callback:
                    progress_callback(f"目录不存在：{directory}", 'error')
                continue
            
            if progress_callback:
                mode_text = "（包含子文件夹）" if include_subdirs else ""
                progress_callback(f"扫描目录：{directory}{mode_text}", 'info')
            
            # 收集 (dirpath, rel_dir, filename)；rel_dir 用于排序分组
            if include_subdirs:
                entries = self._collect_files_recursive(directory, file_type)
            else:
                entries = self._collect_files_flat(directory, file_type)
            
            if not entries:
                if progress_callback:
                    progress_callback(f"目录 {os.path.basename(directory)}: 无目标文件", 'info')
                continue
            
            if progress_callback:
                progress_callback(f"目录 {os.path.basename(directory)}: 找到 {len(entries)} 个文件", 'info')
            
            # 排序：先按相对目录分组（顺序稳定），thumb 文件排后，再按名称
            entries.sort(key=lambda e: (
                e[1],
                1 if 'thumb' in os.path.splitext(e[2])[0].lower() else 0,
                e[2]
            ))

            for _, _, filename in entries:
                f_type = self.get_file_type(filename)
                if f_type == 'image':
                    image_count += 1
                elif f_type in ['video', 'other']:
                    video_count += 1

            folder_name = os.path.basename(directory)
            existing_cache = {}
            counter_cache = {}  # 每个文件夹独立计数器：各文件夹序列单独从 1 开始
            
            for dirpath, _, filename in entries:
                if self.stop_flag:
                    break
                
                extension = os.path.splitext(filename)[1]
                file_type_local = self.get_file_type(filename)
                
                has_date = self.has_date_pattern(filename)
                if enable_date_check and has_date:
                    self.total_skipped += 1
                    self.preview_list.append({
                        'old_path': os.path.join(dirpath, filename),
                        'new_path': None,
                        'status': 'skip_date',
                        'reason': '含日期格式'
                    })
                    continue
                
                if extension.lower() == '.gif':
                    counter_type = 'gif'
                elif file_type_local == 'image':
                    counter_type = 'image'
                elif file_type_local == 'video':
                    counter_type = 'video'
                else:
                    counter_type = 'other'
                
                if dirpath not in counter_cache:
                    counter_cache[dirpath] = {'image': 1, 'video': 1, 'gif': 1, 'other': 1}
                counters = counter_cache[dirpath]
                
                counter = counters[counter_type]
                
                new_filename = self.generate_new_filename(
                    filename, prefix_config, counter, folder_name
                )
                
                new_path = os.path.join(dirpath, new_filename)
                old_path = os.path.join(dirpath, filename)

                if dirpath not in existing_cache:
                    # Windows 文件名不区分大小写，统一用小写比较，避免 A.jpg 与 a.jpg 被误判为不冲突
                    existing_cache[dirpath] = {n.lower() for n in self._snapshot_existing_files(dirpath)}
                existing_files = existing_cache[dirpath]
                old_key = filename.lower()

                conflict_counter = 0
                
                while new_filename.lower() in existing_files and new_filename.lower() != old_key:
                    conflict_counter += 1
                    counter = counters[counter_type] + conflict_counter
                    new_filename = self.generate_new_filename(
                        filename, prefix_config, counter, folder_name
                    )
                    new_path = os.path.join(dirpath, new_filename)

                counters[counter_type] = counter + 1

                if new_filename != filename:
                    existing_files.discard(old_key)
                    existing_files.add(new_filename.lower())

                if old_path == new_path:
                    self.preview_list.append({
                        'old_path': old_path,
                        'new_path': new_path,
                        'status': 'same',
                        'reason': '文件名相同'
                    })
                else:
                    self.preview_list.append({
                        'old_path': old_path,
                        'new_path': new_path,
                        'status': 'rename',
                        'reason': '正常重命名'
                    })
        
        if progress_callback:
            progress_callback(f"预览完成！共找到 {image_count} 个图片，{video_count} 个视频", 'success')
        
        return len(self.preview_list)
    
    def execute_rename(self, progress_callback=None, count_callback=None):
        """执行实际重命名"""
        self.total_renamed = 0
        self.error_count = 0
        self.error_details = []

        batch_records = []
        rename_targets = [it for it in self.preview_list if it['status'] == 'rename']
        total = len(rename_targets)
        report_step = max(1, total // 50)  # 最多 ~50 次进度回调

        if count_callback:
            count_callback(0, total)

        for idx, item in enumerate(rename_targets, 1):
            if self.stop_flag:
                break

            old_path = item['old_path']
            new_path = item['new_path']

            try:
                shutil.move(old_path, new_path)

                record = RenameRecord(
                    old_path=old_path,
                    new_path=new_path,
                    timestamp=datetime.now().isoformat()
                )
                batch_records.append(record)
                self.total_renamed += 1

                if progress_callback and (idx == total or idx % report_step == 0):
                    progress_callback(
                        f"进度 {idx}/{total} - 最新：{os.path.basename(old_path)}",
                        'success'
                    )
                if count_callback and (idx == total or idx % report_step == 0):
                    count_callback(idx, total)

            except Exception as e:
                self.error_count += 1
                error_msg = f"{os.path.basename(old_path)}: {str(e)}"
                self.error_details.append(error_msg)
                if progress_callback:
                    progress_callback(f"错误：{error_msg}", 'error')

        self.rename_records = batch_records
        self.current_record_file = None

        return self.total_renamed, self.error_count, self.error_details
    
    def undo_rename(self, progress_callback=None, count_callback=None):
        """撤销重命名 - 逐条处理，只移除成功的记录"""
        self.stop_flag = False
        self._path_resolve_cache.clear()

        undone = 0
        errors = 0
        fuzzy_matched = 0
        failed_records = []

        total = len(self.rename_records)
        report_step = max(1, total // 50)

        if count_callback:
            count_callback(0, total)

        for idx, record in enumerate(reversed(self.rename_records), 1):
            if self.stop_flag:
                # 被中断的也保留到 failed_records
                failed_records.append(record)
                continue

            try:
                actual_new_path, new_fuzzy = self.resolve_actual_path(record.new_path)

                recorded_new_dir = os.path.dirname(record.new_path)
                recorded_old_dir = os.path.dirname(record.old_path)

                if recorded_new_dir == recorded_old_dir:
                    actual_old_dir = os.path.dirname(actual_new_path)
                    old_dir_fuzzy = new_fuzzy
                else:
                    actual_old_dir, old_dir_fuzzy = self.resolve_actual_path(recorded_old_dir)

                actual_old_path = os.path.join(actual_old_dir, os.path.basename(record.old_path))

                if new_fuzzy or old_dir_fuzzy:
                    fuzzy_matched += 1

                if os.path.exists(actual_new_path):
                    shutil.move(actual_new_path, actual_old_path)
                    undone += 1
                    if progress_callback and (idx == total or idx % report_step == 0):
                        progress_callback(
                            f"进度 {idx}/{total} - 最新：{os.path.basename(record.old_path)}",
                            'success'
                        )
                    if count_callback and (idx == total or idx % report_step == 0):
                        count_callback(idx, total)
                else:
                    errors += 1
                    failed_records.append(record)
                    if progress_callback:
                        progress_callback(
                            f"文件不存在，无法恢复：{actual_new_path}",
                            'error'
                        )

            except Exception as e:
                errors += 1
                failed_records.append(record)
                if progress_callback:
                    progress_callback(f"恢复失败：{str(e)}", 'error')

        if progress_callback and fuzzy_matched > 0:
            progress_callback(f"其中 {fuzzy_matched} 个文件通过模糊匹配定位", 'warning')

        # 只保留失败的记录
        self.rename_records = list(reversed(failed_records))

        return undone, errors
    
    def save_records(self, filepath):
        """保存重命名记录到文件"""
        data = [record.to_dict() for record in self.rename_records]
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.current_record_file = filepath
    
    def load_records(self, filepath):
        """从文件加载重命名记录"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        self.rename_records = [RenameRecord.from_dict(item) for item in data]
        self.current_record_file = filepath
        
        return len(self.rename_records)
    
    def get_record_directories(self):
        """从当前记录中提取涉及的目录列表（去重）"""
        dirs = set()
        for record in self.rename_records:
            parent = os.path.dirname(record.old_path)
            dirs.add(parent)
        return sorted(dirs)
    
    def update_record_file(self):
        """更新记录文件：有记录则覆盖，无记录则删除"""
        if not self.current_record_file:
            return None
        
        if len(self.rename_records) == 0:
            # 全部恢复完毕，删除文件
            try:
                if os.path.exists(self.current_record_file):
                    os.remove(self.current_record_file)
                    removed = self.current_record_file
                    self.current_record_file = None
                    return ('deleted', removed)
            except Exception:
                return ('error', self.current_record_file)
        else:
            # 还有残余，覆盖更新
            try:
                self.save_records(self.current_record_file)
                return ('updated', self.current_record_file)
            except Exception:
                return ('error', self.current_record_file)
    
    def delete_record_file(self):
        """删除当前记录文件"""
        if self.current_record_file and os.path.exists(self.current_record_file):
            try:
                os.remove(self.current_record_file)
                removed = self.current_record_file
                self.current_record_file = None
                return removed
            except Exception:
                return None
        return None
    
    def stop(self):
        """停止操作"""
        self.stop_flag = True
    
    def reset(self):
        """重置状态（不清除 rename_records）"""
        self.stop_flag = False
        self.total_renamed = 0
        self.total_skipped = 0
        self.error_count = 0
        self.error_details = []
        self.preview_list = []




# ==================== GUI 界面 ====================
class RenamerGUI:
    """Tkinter GUI 界面"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("图像视频重命名工具 v4.7")
        
        self.config = ConfigManager()
        self.renamer = FileRenamer()
        self.is_running = False
        self.directories = []
        self.subdirectory_list = []  # 扫描到的子文件夹信息列表

        # 日志队列，用于批量刷新 UI，避免大批量时主线程被刷爆
        self.log_queue = queue.Queue()
        self._log_flush_interval_ms = 200
        
        # 初始化所有 Tkinter 变量
        self.prefix_var = tk.StringVar(value=self.config.get("prefix", "nb01.xyz"))
        self.file_type_var = tk.StringVar(value=self.config.get("file_type", "全部"))
        self.separator_var = tk.StringVar(value=self.config.get("separator", "_"))
        self.number_format_var = tk.StringVar(value=self.config.get("number_format", "04d"))
        self.use_folder_var = tk.BooleanVar(value=self.config.get("use_folder_name", False))
        self.use_original_var = tk.BooleanVar(value=self.config.get("use_original_name", False))
        self.use_file_date_var = tk.BooleanVar(value=self.config.get("use_file_date", False))
        self.use_current_date_var = tk.BooleanVar(value=self.config.get("use_current_date", False))
        self.keep_date_var = tk.BooleanVar(value=self.config.get("keep_date_in_name", False))
        self.add_type_label_var = tk.BooleanVar(value=self.config.get("add_type_label", True))
        self.date_check_var = tk.BooleanVar(value=self.config.get("skip_date_files", True))
        self.include_subdirs_var = tk.BooleanVar(value=self.config.get("include_subdirs", False))
        
        # 设置窗口大小和位置
        width = self.config.get("window_width", 1100)
        height = self.config.get("window_height", 850)
        x = self.config.get("window_x")
        y = self.config.get("window_y")
        
        if x and y:
            self.root.geometry(f'{width}x{height}+{x}+{y}')
        else:
            self.root.geometry(f'{width}x{height}')
            self.center_window()
        
        self.root.minsize(900, 750)
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.setup_styles()
        self.setup_ui()
        self.load_saved_settings()

        # 启动日志批量刷新循环
        self._schedule_log_flush()

        # 启动时自动加载最新记录
        self.auto_load_latest_record()
    
    def setup_styles(self):
        """设置样式"""
        style = ttk.Style()
        style.theme_use('winnative')
        
        style.configure('TLabel', font=('Microsoft YaHei', 9))
        style.configure('Header.TLabel', font=('Microsoft YaHei', 11, 'bold'), foreground='#333333')
        style.configure('Action.TButton', font=('Microsoft YaHei', 9), padding=8)
        style.configure('Success.TLabel', foreground='#2e7d32', font=('Microsoft YaHei', 9, 'bold'))
        style.configure('Error.TLabel', foreground='#c62828', font=('Microsoft YaHei', 9, 'bold'))
        style.configure('Warning.TLabel', foreground='#f57c00', font=('Microsoft YaHei', 9, 'bold'))
        
        style.configure('Treeview', font=('Microsoft YaHei', 9), rowheight=28,
                        background='white', fieldbackground='white')
        style.configure('Treeview.Heading', font=('Microsoft YaHei', 9, 'bold'),
                        background='#e0e0e0', foreground='#333333')
        
        style.configure('TLabelframe', font=('Microsoft YaHei', 9, 'bold'))
        style.configure('TLabelframe.Label', font=('Microsoft YaHei', 9, 'bold'), foreground='#1976D2')
        style.configure('TCheckbutton', font=('Microsoft YaHei', 9))
        style.configure('TButton', font=('Microsoft YaHei', 9), padding=5)
    
    def center_window(self):
        """窗口居中"""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'+{x}+{y}')
    
    def setup_ui(self):
        """设置界面布局 - 左右分栏"""
        # 最外层容器
        main_container = ttk.Frame(self.root, padding="5")
        main_container.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_container.columnconfigure(0, weight=0)  # 左侧固定
        main_container.columnconfigure(1, weight=1)  # 右侧自适应
        main_container.rowconfigure(1, weight=1)      # 内容行撑满
        main_container.rowconfigure(2, weight=0)      # 状态栏
        
        # ===== 0. 标题栏（横跨两列） =====
        title_frame = ttk.Frame(main_container)
        title_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 5))
        
        ttk.Label(title_frame, text="图像视频重命名工具",
                 style='Header.TLabel', padding=3).pack(side=tk.LEFT)
        ttk.Label(title_frame, text="v4.7",
                 font=('Microsoft YaHei', 9), foreground='#666666').pack(side=tk.RIGHT)
        
        # ================================================================
        # 左侧面板
        # ================================================================
        left_panel = ttk.Frame(main_container, width=380)
        left_panel.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(0, 5))
        left_panel.grid_propagate(False)  # 固定宽度
        left_panel.columnconfigure(0, weight=1)
        # 子文件夹选择区域弹性
        left_panel.rowconfigure(2, weight=1)
        
        # ----- 左1. 文件夹管理 -----
        folder_frame = ttk.LabelFrame(left_panel, text="文件夹管理", padding="6")
        folder_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=(0, 4))
        folder_frame.columnconfigure(0, weight=1)
        
        list_container = ttk.Frame(folder_frame)
        list_container.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=2)
        list_container.columnconfigure(0, weight=1)
        
        self.folder_listbox = tk.Listbox(list_container, height=2, font=('Microsoft YaHei', 9),
                                         selectmode=tk.EXTENDED, activestyle='none')
        self.folder_listbox.grid(row=0, column=0, sticky=(tk.W, tk.E))
        
        folder_scroll = ttk.Scrollbar(list_container, orient=tk.HORIZONTAL,
                                      command=self.folder_listbox.xview)
        folder_scroll.grid(row=1, column=0, sticky=(tk.W, tk.E))
        self.folder_listbox.config(xscrollcommand=folder_scroll.set)
        
        btn_row = ttk.Frame(folder_frame)
        btn_row.grid(row=1, column=0, sticky=tk.W, pady=2)
        
        ttk.Button(btn_row, text="添加", command=self.add_folder, width=7).grid(row=0, column=0, padx=2)
        ttk.Button(btn_row, text="移除", command=self.remove_folder, width=7).grid(row=0, column=1, padx=2)
        ttk.Button(btn_row, text="清空", command=self.clear_folders, width=7).grid(row=0, column=2, padx=2)
        ttk.Button(btn_row, text="默认路径", command=self.use_default_folder, width=8).grid(row=0, column=3, padx=2)
        
        # ----- 左2. 重命名设置 -----
        setting_frame = ttk.LabelFrame(left_panel, text="重命名设置", padding="6")
        setting_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=4)
        setting_frame.columnconfigure(1, weight=1)
        setting_frame.columnconfigure(3, weight=1)
        
        # 前缀 + 类型
        ttk.Label(setting_frame, text="前缀:").grid(row=0, column=0, sticky=tk.W, padx=2, pady=2)
        ttk.Entry(setting_frame, textvariable=self.prefix_var, width=16,
                 font=('Microsoft YaHei', 9)).grid(row=0, column=1, sticky=tk.W, padx=2, pady=2)
        
        ttk.Label(setting_frame, text="类型:").grid(row=0, column=2, sticky=tk.W, padx=(8, 2), pady=2)
        ttk.Combobox(setting_frame, textvariable=self.file_type_var,
                    values=["全部", "仅图片", "仅视频"], width=8, state="readonly",
                    font=('Microsoft YaHei', 9)).grid(row=0, column=3, sticky=tk.W, padx=2, pady=2)
        
        # 分隔符 + 序号
        ttk.Label(setting_frame, text="分隔符:").grid(row=1, column=0, sticky=tk.W, padx=2, pady=2)
        ttk.Entry(setting_frame, textvariable=self.separator_var, width=6,
                 font=('Microsoft YaHei', 9)).grid(row=1, column=1, sticky=tk.W, padx=2, pady=2)
        
        ttk.Label(setting_frame, text="序号:").grid(row=1, column=2, sticky=tk.W, padx=(8, 2), pady=2)
        ttk.Combobox(setting_frame, textvariable=self.number_format_var,
                    values=["03d", "04d", "05d", "06d"], width=8, state="readonly",
                    font=('Microsoft YaHei', 9)).grid(row=1, column=3, sticky=tk.W, padx=2, pady=2)
        
        # 选项（紧凑两列排列）
        opt_frame = ttk.LabelFrame(setting_frame, text="选项", padding="4")
        opt_frame.grid(row=2, column=0, columnspan=4, sticky=(tk.W, tk.E), pady=4)
        
        ttk.Checkbutton(opt_frame, text="使用文件夹名",
                       variable=self.use_folder_var).grid(row=0, column=0, sticky=tk.W, padx=4, pady=1)
        ttk.Checkbutton(opt_frame, text="添加类型标签",
                       variable=self.add_type_label_var).grid(row=0, column=1, sticky=tk.W, padx=4, pady=1)
        
        ttk.Checkbutton(opt_frame, text="保留原文件名",
                       variable=self.use_original_var).grid(row=1, column=0, sticky=tk.W, padx=4, pady=1)
        ttk.Checkbutton(opt_frame, text="使用文件日期",
                       variable=self.use_file_date_var).grid(row=1, column=1, sticky=tk.W, padx=4, pady=1)
        
        ttk.Checkbutton(opt_frame, text="使用当前日期",
                       variable=self.use_current_date_var).grid(row=2, column=0, sticky=tk.W, padx=4, pady=1)
        ttk.Checkbutton(opt_frame, text="保留名中日期",
                       variable=self.keep_date_var).grid(row=2, column=1, sticky=tk.W, padx=4, pady=1)
        
        ttk.Checkbutton(opt_frame, text="跳过含日期文件(YYYY-MM-DD)",
                       variable=self.date_check_var).grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=4, pady=1)
        
        ttk.Checkbutton(opt_frame, text="包含子文件夹(递归处理所选文件夹下的全部子文件夹)",
                       variable=self.include_subdirs_var,
                       command=self.on_include_subdirs_changed).grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=4, pady=1)
        
        # ----- 左3. 子文件夹选择（弹性区域） -----
        self.subdir_frame = ttk.LabelFrame(left_panel, text="子文件夹选择（蓝色高亮=已选中）", padding="5")
        self.subdir_frame.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=4)
        self.subdir_frame.columnconfigure(0, weight=1)
        self.subdir_frame.rowconfigure(1, weight=1)
        self.subdir_frame.grid_remove()  # 初始隐藏
        
        subdir_btn_frame = ttk.Frame(self.subdir_frame)
        subdir_btn_frame.grid(row=0, column=0, sticky=tk.W, pady=2)
        
        ttk.Button(subdir_btn_frame, text="全选", command=self.subdir_select_all, width=6).grid(row=0, column=0, padx=2)
        ttk.Button(subdir_btn_frame, text="取消", command=self.subdir_select_none, width=6).grid(row=0, column=1, padx=2)
        ttk.Button(subdir_btn_frame, text="反选", command=self.subdir_invert, width=6).grid(row=0, column=2, padx=2)
        
        self.subdir_count_label = ttk.Label(subdir_btn_frame, text="",
                                            font=('Microsoft YaHei', 8), foreground='#666666')
        self.subdir_count_label.grid(row=0, column=3, padx=8)
        
        subdir_list_container = ttk.Frame(self.subdir_frame)
        subdir_list_container.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=2)
        subdir_list_container.columnconfigure(0, weight=1)
        subdir_list_container.rowconfigure(0, weight=1)
        
        self.subdir_listbox = tk.Listbox(
            subdir_list_container, font=('Microsoft YaHei', 9),
            selectmode=tk.EXTENDED, activestyle='none',
            selectbackground='#0078D7', selectforeground='white',
            bg='white', relief=tk.SUNKEN, borderwidth=1
        )
        self.subdir_listbox.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        subdir_sy = ttk.Scrollbar(subdir_list_container, orient=tk.VERTICAL,
                                  command=self.subdir_listbox.yview)
        subdir_sy.grid(row=0, column=1, sticky=(tk.N, tk.S))
        self.subdir_listbox.config(yscrollcommand=subdir_sy.set)
        
        subdir_sx = ttk.Scrollbar(subdir_list_container, orient=tk.HORIZONTAL,
                                  command=self.subdir_listbox.xview)
        subdir_sx.grid(row=1, column=0, sticky=(tk.W, tk.E))
        self.subdir_listbox.config(xscrollcommand=subdir_sx.set)
        
        # ----- 左4. 操作按钮 -----
        action_frame = ttk.LabelFrame(left_panel, text="操作", padding="6")
        action_frame.grid(row=3, column=0, sticky=(tk.W, tk.E), pady=4)
        action_frame.columnconfigure(0, weight=1)
        action_frame.columnconfigure(1, weight=1)
        action_frame.columnconfigure(2, weight=1)
        
        self.preview_btn = ttk.Button(action_frame, text="扫描文件夹",
                                      command=self.scan_folders, style='Action.TButton')
        self.preview_btn.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=2, pady=2)
        
        self.generate_preview_btn = ttk.Button(action_frame, text="生成预览",
                                               command=self.generate_preview, style='Action.TButton')
        self.generate_preview_btn.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=2, pady=2)
        self.generate_preview_btn.config(state=tk.DISABLED)
        
        self.start_btn = ttk.Button(action_frame, text="开始重命名",
                                    command=self.start_rename, style='Action.TButton')
        self.start_btn.grid(row=0, column=2, sticky=(tk.W, tk.E), padx=2, pady=2)
        self.start_btn.config(state=tk.DISABLED)
        
        self.stop_btn = ttk.Button(action_frame, text="停止",
                                   command=self.stop_rename, style='Action.TButton')
        self.stop_btn.grid(row=1, column=0, sticky=(tk.W, tk.E), padx=2, pady=2)
        self.stop_btn.config(state=tk.DISABLED)
        
        self.load_record_btn = ttk.Button(action_frame, text="加载记录",
                                          command=self.load_record_file, style='Action.TButton')
        self.load_record_btn.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=2, pady=2)
        
        self.undo_btn = ttk.Button(action_frame, text="撤销重命名",
                                   command=self.undo_rename, style='Action.TButton')
        self.undo_btn.grid(row=1, column=2, sticky=(tk.W, tk.E), padx=2, pady=2)
        
        self.record_label = ttk.Label(action_frame, text="记录：0 条",
                                      font=('Microsoft YaHei', 8), foreground='#666666')
        self.record_label.grid(row=2, column=0, columnspan=3, sticky=tk.W, padx=4, pady=(2, 0))
        
        # ================================================================
        # 右侧面板
        # ================================================================
        right_panel = ttk.Frame(main_container)
        right_panel.grid(row=1, column=1, sticky=(tk.W, tk.E, tk.N, tk.S))
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(0, weight=3)  # 预览占3
        right_panel.rowconfigure(1, weight=1)  # 日志占1
        
        # ----- 右1. 预览列表 -----
        preview_frame = ttk.LabelFrame(right_panel, text="预览列表", padding="5")
        preview_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 4))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        
        columns = ('status', 'old_name', 'new_name', 'path')
        
        self.preview_tree = ttk.Treeview(preview_frame, columns=columns, show='headings', height=12)
        
        self.preview_tree.heading('status', text='状态')
        self.preview_tree.heading('old_name', text='原文件名')
        self.preview_tree.heading('new_name', text='新文件名')
        self.preview_tree.heading('path', text='路径')
        
        self.preview_tree.column('status', width=60, anchor='center', minwidth=50)
        self.preview_tree.column('old_name', width=200, anchor='w', minwidth=120)
        self.preview_tree.column('new_name', width=200, anchor='w', minwidth=120)
        self.preview_tree.column('path', width=300, anchor='w', minwidth=150)
        
        tree_sy = ttk.Scrollbar(preview_frame, orient=tk.VERTICAL,
                                command=self.preview_tree.yview)
        self.preview_tree.configure(yscrollcommand=tree_sy.set)
        
        tree_sx = ttk.Scrollbar(preview_frame, orient=tk.HORIZONTAL,
                                command=self.preview_tree.xview)
        self.preview_tree.configure(xscrollcommand=tree_sx.set)
        
        self.preview_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        tree_sy.grid(row=0, column=1, sticky=(tk.N, tk.S))
        tree_sx.grid(row=1, column=0, sticky=(tk.W, tk.E))
        
        self.preview_tree.tag_configure('normal', foreground='#1976D2')
        self.preview_tree.tag_configure('warning', foreground='#f57c00')
        self.preview_tree.tag_configure('info', foreground='#757575')
        
        self.preview_stat_label = ttk.Label(preview_frame, text="",
                                            font=('Microsoft YaHei', 9), foreground='#333333')
        self.preview_stat_label.grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        
        # ----- 右2. 处理日志 -----
        log_frame = ttk.LabelFrame(right_panel, text="处理日志", padding="5")
        log_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=6, wrap=tk.WORD,
                                                  state=tk.DISABLED, font=('Consolas', 9))
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # ================================================================
        # 底部状态栏（横跨两列）
        # ================================================================
        stat_frame = ttk.Frame(main_container, padding="5")
        stat_frame.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(3, 0))
        stat_frame.columnconfigure(1, weight=1)
        
        self.stat_label = ttk.Label(stat_frame, text="就绪 | 已重命名：0 | 已跳过：0 | 错误：0",
                                   font=('Microsoft YaHei', 9, 'bold'))
        self.stat_label.grid(row=0, column=0, sticky=tk.W)
        
        self.progress_var = tk.IntVar(value=0)
        self.progress_bar = ttk.Progressbar(stat_frame, variable=self.progress_var,
                                           maximum=100, mode='determinate', length=250)
        self.progress_bar.grid(row=0, column=1, sticky=tk.E, padx=(20, 0))

    
    # ==================== 子文件夹选择方法 ====================
    
    def scan_folders(self):
        """扫描文件夹，列出子文件夹供用户选择（后台线程）"""
        if not self.directories:
            messagebox.showwarning("提示", "请先添加要处理的文件夹！")
            return

        if self.is_running:
            return

        self.renamer.stop_flag = False  # 上次点过“停止”会残留标志，导致递归扫描直接返回空结果

        self.log("=" * 60, 'info')
        include_subdirs = self.include_subdirs_var.get()
        self.log("开始扫描子文件夹..." + ("（包含子文件夹）" if include_subdirs else ""), 'info')

        file_type_map = {"全部": "all", "仅图片": "image", "仅视频": "video"}
        file_type = file_type_map.get(self.file_type_var.get(), "all")

        directories_snapshot = list(self.directories)

        self.is_running = True
        self.preview_btn.config(state=tk.DISABLED)
        self.generate_preview_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self.progress_bar.config(mode='indeterminate')
        self.progress_bar.start(50)

        def worker():
            collected = []
            try:
                for directory in directories_snapshot:
                    self.log(f"扫描：{directory}", 'info')
                    subdirs = self.renamer.scan_subdirectories(directory, file_type, include_subdirs)
                    collected.extend(subdirs)
            except Exception as e:
                err_msg = str(e)
                self.log(f"扫描出错：{err_msg}", 'error')
                self.root.after(0, lambda: self._scan_finish_error(err_msg))
                return

            self.root.after(0, lambda: self._scan_finish_ok(collected))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_finish_error(self, msg):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        self.progress_var.set(0)
        self.is_running = False
        self.preview_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.DISABLED)
        messagebox.showerror("错误", f"扫描出错：{msg}")

    def _scan_finish_ok(self, collected):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        self.progress_var.set(0)
        self.is_running = False
        self.preview_btn.config(state=tk.NORMAL)

        self.subdirectory_list = collected

        if not self.subdirectory_list:
            messagebox.showinfo("提示", "未找到包含目标文件的子文件夹！")
            self.log("未找到包含目标文件的子文件夹", 'warning')
            self.subdir_frame.grid_remove()
            self.generate_preview_btn.config(state=tk.DISABLED)
            self.start_btn.config(state=tk.DISABLED)
            return

        # 填充子文件夹列表
        self.subdir_listbox.delete(0, tk.END)

        for info in self.subdirectory_list:
            parts = []
            if info['image_count'] > 0:
                parts.append(f"{info['image_count']}张图片")
            if info['video_count'] > 0:
                parts.append(f"{info['video_count']}个视频")
            count_str = ", ".join(parts)

            display_text = f"{info['name']}    ({count_str})"
            self.subdir_listbox.insert(tk.END, display_text)

        self.subdir_select_all()
        self.subdir_frame.grid()

        total_imgs = sum(d['image_count'] for d in self.subdirectory_list)
        total_vids = sum(d['video_count'] for d in self.subdirectory_list)
        suffix = "（含子文件夹）" if self.include_subdirs_var.get() else ""
        self.subdir_count_label.config(
            text=f"共 {len(self.subdirectory_list)} 个文件夹{suffix} | {total_imgs} 张图片, {total_vids} 个视频"
        )

        self.log(
            f"扫描完成：{len(self.subdirectory_list)} 个文件夹，{total_imgs} 张图片，{total_vids} 个视频",
            'success'
        )

        self.generate_preview_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.DISABLED)
    
    def subdir_select_all(self):
        """全选子文件夹"""
        self.subdir_listbox.select_set(0, tk.END)
    
    def subdir_select_none(self):
        """取消全选"""
        self.subdir_listbox.select_clear(0, tk.END)
    
    def subdir_invert(self):
        """反选"""
        total = self.subdir_listbox.size()
        current = set(self.subdir_listbox.curselection())
        self.subdir_listbox.select_clear(0, tk.END)
        for i in range(total):
            if i not in current:
                self.subdir_listbox.select_set(i)
    
    def get_selected_directories(self):
        """获取用户选中的子文件夹路径列表"""
        selection = self.subdir_listbox.curselection()
        if not selection:
            return []
        return [self.subdirectory_list[i]['path'] for i in selection]
    
    def on_include_subdirs_changed(self):
        """包含子文件夹选项变化时，使旧的扫描统计与预览结果失效"""
        if self.subdirectory_list:
            self.log("包含子文件夹设置已更改，当前统计可能不准确，请重新扫描", 'warning')
            self.generate_preview_btn.config(state=tk.DISABLED)
            self.start_btn.config(state=tk.DISABLED)
    
    # ==================== 基础方法 ====================
    
    def load_saved_settings(self):
        """加载保存的设置"""
        default_folder = self.config.get("default_folder", r"Y:\1")
        self.directories = [default_folder]
        self.folder_listbox.insert(tk.END, default_folder)
        self.log(f"已加载默认路径：{default_folder}", 'info')
        self.log(f"支持扩展名：{self.renamer.all_extensions}", 'info')
    
    def save_current_settings(self):
        """保存当前设置到配置文件"""
        try:
            settings = {
                "default_folder": self.directories[0] if self.directories else r"Y:\1",
                "prefix": self.prefix_var.get(),
                "file_type": self.file_type_var.get(),
                "separator": self.separator_var.get(),
                "number_format": self.number_format_var.get(),
                "use_folder_name": self.use_folder_var.get(),
                "use_original_name": self.use_original_var.get(),
                "use_file_date": self.use_file_date_var.get(),
                "use_current_date": self.use_current_date_var.get(),
                "keep_date_in_name": self.keep_date_var.get(),
                "add_type_label": self.add_type_label_var.get(),
                "skip_date_files": self.date_check_var.get(),
                "include_subdirs": self.include_subdirs_var.get(),
                "original_name_max_len": self.config.get("original_name_max_len", 0),
                "window_width": self.root.winfo_width(),
                "window_height": self.root.winfo_height(),
                "window_x": self.root.winfo_x(),
                "window_y": self.root.winfo_y()
            }
            self.config.save_config(settings)
        except Exception as e:
            print(f"保存设置失败：{e}")
    
    def add_folder(self):
        """添加文件夹"""
        folder = filedialog.askdirectory(initialdir=self.config.get("default_folder", r"Y:\1"))
        if folder and folder not in self.directories:
            self.directories.append(folder)
            self.folder_listbox.insert(tk.END, folder)
            self.log(f"已添加文件夹：{folder}", 'info')
    
    def remove_folder(self):
        """移除选中文件夹"""
        selection = self.folder_listbox.curselection()
        if selection:
            for index in reversed(selection):
                folder = self.folder_listbox.get(index)
                if folder in self.directories:
                    self.directories.remove(folder)
                self.folder_listbox.delete(index)
            self.log("已移除选中的文件夹", 'info')
    
    def clear_folders(self):
        """清空文件夹列表"""
        self.directories.clear()
        self.folder_listbox.delete(0, tk.END)
        self.log("已清空文件夹列表", 'info')
    
    def use_default_folder(self):
        """使用默认路径"""
        default_folder = self.config.get("default_folder", r"Y:\1")
        if default_folder not in self.directories:
            self.directories = [default_folder]
            self.folder_listbox.delete(0, tk.END)
            self.folder_listbox.insert(tk.END, default_folder)
            self.log(f"已使用默认路径：{default_folder}", 'info')
    
    def log(self, message, level='info'):
        """添加日志（线程安全，写入队列由 UI 周期性 flush）"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        level_prefix = {
            'info': '[INFO]',
            'success': '[OK]',
            'warning': '[WARN]',
            'error': '[ERROR]'
        }
        prefix = level_prefix.get(level, '[INFO]')
        self.log_queue.put(f"[{timestamp}] {prefix} {message}\n")

    def _schedule_log_flush(self):
        """周期性把日志队列里的内容批量刷到文本框"""
        try:
            self._flush_log_queue()
        except Exception:
            return
        try:
            self.root.after(self._log_flush_interval_ms, self._schedule_log_flush)
        except tk.TclError:
            return

    def _flush_log_queue(self):
        """一次性取出队列中的全部日志写入文本框"""
        if self.log_queue.empty():
            return
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if not lines:
            return
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, ''.join(lines))
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def clear_preview(self):
        """清空预览列表"""
        for item in self.preview_tree.get_children():
            self.preview_tree.delete(item)
        self.preview_stat_label.config(text="")
    
    def update_preview(self):
        """更新预览列表"""
        self.clear_preview()
        
        rename_count = 0
        skip_count = 0
        same_count = 0
        
        for item in self.renamer.preview_list:
            status_map = {
                'rename': ('重命名', 'normal'),
                'skip_date': ('跳过', 'warning'),
                'same': ('相同', 'info')
            }
            
            status_text, tag = status_map.get(item['status'], ('未知', 'normal'))
            
            old_name = os.path.basename(item['old_path'])
            new_name = os.path.basename(item['new_path']) if item['new_path'] else '-'
            path = os.path.dirname(item['old_path'])
            
            self.preview_tree.insert('', tk.END, values=(status_text, old_name, new_name, path), tags=(tag,))
            
            if item['status'] == 'rename':
                rename_count += 1
            elif item['status'] == 'skip_date':
                skip_count += 1
            else:
                same_count += 1
        
        # 更新预览统计
        self.preview_stat_label.config(
            text=f"共 {rename_count + skip_count + same_count} 个文件 | "
                 f"将重命名：{rename_count} | 跳过：{skip_count} | 相同：{same_count}"
        )
        
        return rename_count, skip_count, same_count
    
    def update_stats(self, renamed=0, skipped=0, errors=0):
        """更新统计信息"""
        text = f"就绪 | 已重命名：{renamed} | 已跳过：{skipped} | 错误：{errors}"
        self.stat_label.config(text=text)
    
    def get_prefix_config(self):
        """获取前缀配置"""
        return {
            'custom_prefix': self.prefix_var.get(),
            'use_custom_prefix': True,
            'use_folder_name': self.use_folder_var.get(),
            'use_original_name': self.use_original_var.get(),
            'use_file_date': self.use_file_date_var.get(),
            'use_current_date': self.use_current_date_var.get(),
            'keep_date_in_name': self.keep_date_var.get(),
            'add_type_label': self.add_type_label_var.get(),
            'separator': self.separator_var.get(),
            'number_format': self.number_format_var.get(),
            'original_name_max_len': self.config.get('original_name_max_len', 0)
        }

    # ==================== 预览和执行 ====================
    
    def generate_preview(self):
        """根据用户选中的子文件夹生成预览（后台线程）"""
        selected_dirs = self.get_selected_directories()

        if not selected_dirs:
            messagebox.showwarning("提示", "请至少选择一个子文件夹！")
            return

        if self.is_running:
            return

        self.log("=" * 60, 'info')
        include_subdirs = self.include_subdirs_var.get()
        self.log(
            f"开始生成预览，已选择 {len(selected_dirs)} 个文件夹"
            + ("（包含子文件夹）" if include_subdirs else "") + "...",
            'info'
        )

        self.renamer.reset()
        self.clear_preview()

        prefix_config = self.get_prefix_config()
        file_type_map = {"全部": "all", "仅图片": "image", "仅视频": "video"}
        file_type = file_type_map.get(self.file_type_var.get(), "all")
        enable_date_check = self.date_check_var.get()

        self.is_running = True
        self.preview_btn.config(state=tk.DISABLED)
        self.generate_preview_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self.progress_bar.config(mode='indeterminate')
        self.progress_bar.start(50)

        def worker():
            try:
                total = self.renamer.generate_preview(
                    directories=selected_dirs,
                    prefix_config=prefix_config,
                    file_type=file_type,
                    enable_date_check=enable_date_check,
                    include_subdirs=include_subdirs,
                    progress_callback=lambda msg, level: self.log(msg, level)
                )
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self._preview_finish_error(err_msg))
                return

            self.root.after(0, lambda: self._preview_finish_ok(total))

        threading.Thread(target=worker, daemon=True).start()

    def _preview_finish_error(self, msg):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        self.progress_var.set(0)
        self.is_running = False
        self.preview_btn.config(state=tk.NORMAL)
        self.generate_preview_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.DISABLED)
        self.log(f"预览出错：{msg}", 'error')
        messagebox.showerror("错误", f"预览出错：{msg}")

    def _preview_finish_ok(self, total):
        self.progress_bar.stop()
        self.progress_bar.config(mode='determinate')
        self.progress_var.set(0)
        self.is_running = False
        self.preview_btn.config(state=tk.NORMAL)
        self.generate_preview_btn.config(state=tk.NORMAL)

        rename_count, skip_count, same_count = self.update_preview()

        self.log(f"预览完成！共扫描 {total} 个文件", 'success')
        self.log(
            f"将要重命名：{rename_count} | 跳过：{skip_count} | 相同：{same_count}",
            'info'
        )

        if rename_count > 0:
            self.start_btn.config(state=tk.NORMAL)
            self.update_stats(renamed=rename_count, skipped=skip_count, errors=0)
        else:
            self.start_btn.config(state=tk.DISABLED)
            messagebox.showinfo("提示", "没有需要重命名的文件！")
    
    def preview_rename(self):
        """兼容旧调用：扫描+预览一步完成"""
        self.scan_folders()
        if self.subdirectory_list:
            self.generate_preview()
    
    def start_rename(self):
        """开始重命名"""
        if self.is_running or not self.renamer.preview_list:
            return
        
        rename_count = sum(1 for item in self.renamer.preview_list if item['status'] == 'rename')
        if rename_count == 0:
            messagebox.showwarning("提示", "没有需要重命名的文件！")
            return
        
        confirm = messagebox.askyesno(
            "确认",
            f"将要重命名 {rename_count} 个文件，是否继续？\n\n注意：重命名记录将自动保存！"
        )
        if not confirm:
            return
        
        self.is_running = True
        self.preview_btn.config(state=tk.DISABLED)
        self.generate_preview_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.undo_btn.config(state=tk.DISABLED)
        self.load_record_btn.config(state=tk.DISABLED)
        rename_total = sum(1 for it in self.renamer.preview_list if it['status'] == 'rename')
        self.progress_bar.config(mode='determinate', maximum=max(1, rename_total))
        self.progress_var.set(0)

        self.log("=" * 60, 'info')
        self.log("开始执行重命名...", 'info')

        thread = threading.Thread(target=self.run_rename)
        thread.daemon = True
        thread.start()

    def _on_count(self, done, total):
        """线程安全地更新进度条（只更新值，不重设 maximum）"""
        self.root.after(0, lambda: self.progress_var.set(done))

    def run_rename(self):
        """执行重命名（后台线程）"""
        try:
            renamed, errors, error_details = self.renamer.execute_rename(
                progress_callback=lambda msg, level: self.log(msg, level),
                count_callback=self._on_count
            )
            
            def auto_save_records():
                if renamed > 0:
                    script_dir = os.path.dirname(os.path.abspath(__file__))
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    record_filename = f"图像视频重命名记录_{timestamp}.json"
                    record_path = os.path.join(script_dir, record_filename)
                    
                    try:
                        self.renamer.save_records(record_path)
                        self.log(f"重命名记录已自动保存：{record_filename}", 'success')
                    except Exception as e:
                        self.log(f"保存记录失败：{str(e)}", 'error')
            
            def finish():
                self.progress_bar.stop()
                self.progress_var.set(self.progress_bar['maximum'])
                self.is_running = False
                self.preview_btn.config(state=tk.NORMAL)
                self.generate_preview_btn.config(state=tk.NORMAL)
                # 预览里的旧路径已经失效，必须重新生成预览才能再次重命名；
                # 否则再次点击会因找不到文件而报错，并覆盖内存中的撤销记录
                self.start_btn.config(state=tk.DISABLED)
                self.renamer.preview_list = []
                self.renamer.stop_flag = False
                self.stop_btn.config(state=tk.DISABLED)
                self.undo_btn.config(state=tk.NORMAL)
                self.load_record_btn.config(state=tk.NORMAL)
                
                auto_save_records()
                
                self.log("=" * 60, 'success')
                self.log(f"重命名完成！", 'success')
                self.log(f"成功：{renamed} | 错误：{errors}", 'info')
                
                if error_details:
                    self.log("\n错误详情:", 'error')
                    for detail in error_details:
                        self.log(f"  - {detail}", 'error')
                
                self.update_stats(renamed=renamed, skipped=self.renamer.total_skipped, errors=errors)
                self.update_record_label()
                
                if errors == 0 and renamed > 0:
                    messagebox.showinfo("完成",
                        f"成功重命名 {renamed} 个文件！\n"
                        f"记录已自动保存，可使用撤销功能恢复。\n"
                        f"即使重启程序也能撤销。")
                elif errors > 0:
                    messagebox.showwarning("完成", f"重命名完成，但有 {errors} 个错误，请查看日志。")
            
            self.root.after(0, finish)
            
        except Exception as e:
            err_msg = str(e)
            def error_finish():
                self.progress_bar.stop()
                self.progress_var.set(0)
                self.is_running = False
                self.preview_btn.config(state=tk.NORMAL)
                self.generate_preview_btn.config(state=tk.NORMAL)
                self.start_btn.config(state=tk.DISABLED)
                self.stop_btn.config(state=tk.DISABLED)
                self.load_record_btn.config(state=tk.NORMAL)
                self.renamer.stop_flag = False
                self.log(f"脚本执行出错：{err_msg}", 'error')
                messagebox.showerror("错误", f"脚本执行出错：{err_msg}")
            
            self.root.after(0, error_finish)
    
    def stop_rename(self):
        """停止操作"""
        if self.is_running:
            self.renamer.stop()
            self.log("正在停止操作...", 'warning')
    
    # ==================== 撤销相关 ====================
    
    def auto_load_latest_record(self):
        """启动时自动加载最新的重命名记录"""
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            
            record_files = []
            with os.scandir(script_dir) as it:
                for entry in it:
                    if (entry.is_file(follow_symlinks=False)
                            and entry.name.startswith("图像视频重命名记录_")
                            and entry.name.endswith(".json")):
                        record_files.append((entry.path, entry.stat().st_mtime, entry.name))
            
            if not record_files:
                self.log("未找到历史重命名记录", 'info')
                self.update_record_label()
                return
            
            record_files.sort(key=lambda x: x[1], reverse=True)
            latest_path, _, latest_name = record_files[0]
            
            count = self.renamer.load_records(latest_path)
            
            if count > 0:
                self.log(f"已自动加载记录：{latest_name}（{count} 条）", 'success')
                
                record_dirs = self.renamer.get_record_directories()
                if record_dirs:
                    dir_names = [os.path.basename(d) for d in record_dirs]
                    self.log(f"记录涉及 {len(record_dirs)} 个目录：{', '.join(dir_names[:5])}"
                            + ("..." if len(dir_names) > 5 else ""), 'info')
                
                self.update_record_label()
                
                if len(record_files) > 1:
                    self.log(f"共发现 {len(record_files)} 个历史记录文件，已加载最新的", 'info')
            else:
                self.log(f"记录文件为空：{latest_name}", 'warning')
                
        except Exception as e:
            self.log(f"自动加载记录失败：{str(e)}", 'error')
    
    def load_record_file(self):
        """手动选择并加载记录文件"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        filepath = filedialog.askopenfilename(
            title="选择重命名记录文件",
            initialdir=script_dir,
            filetypes=[
                ("JSON 记录文件", "*.json"),
                ("所有文件", "*.*")
            ]
        )
        
        if not filepath:
            return
        
        try:
            count = self.renamer.load_records(filepath)
            
            self.log("=" * 60, 'info')
            self.log(f"已加载记录文件：{os.path.basename(filepath)}", 'success')
            self.log(f"共 {count} 条重命名记录", 'info')
            
            record_dirs = self.renamer.get_record_directories()
            if record_dirs:
                self.log(f"记录涉及 {len(record_dirs)} 个目录：", 'info')
                for d in record_dirs:
                    exists = os.path.exists(d)
                    if exists:
                        self.log(f"  ✓ {d}", 'info')
                    else:
                        actual, fuzzy = self.renamer.resolve_actual_path(d)
                        if fuzzy:
                            self.log(f"  ~ {d}", 'warning')
                            self.log(f"    → 匹配到: {actual}", 'warning')
                        else:
                            self.log(f"  ✗ {d}（目录不存在）", 'error')
                
                if messagebox.askyesno("自动填充目录",
                    f"记录涉及 {len(record_dirs)} 个目录，\n是否自动填充到文件夹列表？"):
                    self.auto_fill_directories(record_dirs)
            
            self.update_record_label()
            
        except json.JSONDecodeError:
            messagebox.showerror("错误", "文件格式错误，不是有效的 JSON 文件！")
            self.log("加载失败：文件格式错误", 'error')
        except Exception as e:
            messagebox.showerror("错误", f"加载记录失败：{str(e)}")
            self.log(f"加载失败：{str(e)}", 'error')
    
    def auto_fill_directories(self, record_dirs):
        """从记录中自动填充目录到文件夹列表"""
        parent_dirs = set()
        for d in record_dirs:
            if os.path.exists(d):
                parent_dirs.add(d)
            else:
                actual, fuzzy = self.renamer.resolve_actual_path(d)
                if fuzzy and os.path.exists(actual):
                    parent_dirs.add(actual)
                else:
                    parent_dirs.add(d)
        
        if parent_dirs:
            try:
                common = os.path.commonpath(list(parent_dirs))
                if common in parent_dirs:
                    dirs_to_add = [common]
                else:
                    dirs_to_add = [common]
            except ValueError:
                dirs_to_add = list(parent_dirs)
        else:
            dirs_to_add = list(record_dirs)
        
        added = 0
        for d in dirs_to_add:
            if d not in self.directories:
                self.directories.append(d)
                self.folder_listbox.insert(tk.END, d)
                added += 1
        
        if added > 0:
            self.log(f"已自动添加 {added} 个目录到文件夹列表", 'success')
        else:
            self.log("目录已在列表中，无需重复添加", 'info')
    
    def update_record_label(self):
        """更新记录数标签"""
        count = len(self.renamer.rename_records)
        if count > 0:
            source = ""
            if self.renamer.current_record_file:
                source = f" | {os.path.basename(self.renamer.current_record_file)}"
            self.record_label.config(
                text=f"记录：{count} 条{source}",
                foreground='#2e7d32'
            )
        else:
            self.record_label.config(
                text="记录：0 条",
                foreground='#666666'
            )
    
    def undo_rename(self):
        """撤销重命名"""
        if not self.renamer.rename_records:
            if messagebox.askyesno("提示",
                "当前没有可撤销的记录。\n是否加载历史记录文件？"):
                self.load_record_file()
            return
        
        record_count = len(self.renamer.rename_records)
        record_dirs = self.renamer.get_record_directories()
        dir_info = "\n".join([f"  • {os.path.basename(d)}" for d in record_dirs[:5]])
        if len(record_dirs) > 5:
            dir_info += f"\n  ...等 {len(record_dirs)} 个目录"
        
        confirm = messagebox.askyesno(
            "确认撤销",
            f"将撤销 {record_count} 个文件的重命名\n\n"
            f"涉及目录：\n{dir_info}\n\n"
            f"如果文件夹名已添加标注后缀，将自动模糊匹配。\n\n"
            f"是否继续？"
        )
        if not confirm:
            return
        
        self.log("=" * 60, 'info')
        self.log("开始撤销重命名...", 'info')
        
        self.is_running = True
        self.undo_btn.config(state=tk.DISABLED)
        self.load_record_btn.config(state=tk.DISABLED)
        self.preview_btn.config(state=tk.DISABLED)
        self.generate_preview_btn.config(state=tk.DISABLED)
        undo_total = len(self.renamer.rename_records)
        self.progress_bar.config(mode='determinate', maximum=max(1, undo_total))
        self.progress_var.set(0)

        def run_undo():
            try:
                undone, errors = self.renamer.undo_rename(
                    progress_callback=lambda msg, level: self.log(msg, level),
                    count_callback=self._on_count
                )
                
                def finish():
                    self.progress_bar.stop()
                    self.progress_var.set(self.progress_bar['maximum'])
                    self.is_running = False
                    self.undo_btn.config(state=tk.NORMAL)
                    self.load_record_btn.config(state=tk.NORMAL)
                    self.preview_btn.config(state=tk.NORMAL)
                    self.generate_preview_btn.config(state=tk.NORMAL)
                    
                    self.log("=" * 60, 'success')
                    self.log(f"撤销完成！恢复 {undone} 个文件，失败 {errors} 个", 'success')
                    self.update_stats(renamed=0, skipped=0, errors=errors)
                    
                    # 处理记录文件
                    remaining = len(self.renamer.rename_records)
                    
                    if remaining == 0:
                        # 全部恢复成功
                        result = self.renamer.update_record_file()
                        if result and result[0] == 'deleted':
                            self.log(f"已删除记录文件：{os.path.basename(result[1])}", 'info')
                        
                        self.update_record_label()
                        messagebox.showinfo("完成", f"已成功恢复全部 {undone} 个文件！")
                    
                    elif undone > 0:
                        # 部分成功，覆盖更新记录文件
                        result = self.renamer.update_record_file()
                        if result and result[0] == 'updated':
                            self.log(
                                f"还有 {remaining} 条记录未恢复，记录文件已更新：{os.path.basename(result[1])}",
                                'warning'
                            )
                        
                        self.update_record_label()
                        messagebox.showwarning("部分完成",
                            f"恢复 {undone} 个文件，{errors} 个失败。\n"
                            f"剩余 {remaining} 条记录已保存，可再次尝试撤销。")
                    
                    else:
                        # 全部失败，不动记录文件
                        self.update_record_label()
                        messagebox.showerror("失败",
                            f"全部 {errors} 个文件恢复失败。\n"
                            f"记录已保留，请检查文件路径后重试。")
                
                self.root.after(0, finish)
                
            except Exception as e:
                err_msg = str(e)
                def error_finish():
                    self.progress_bar.stop()
                    self.progress_var.set(0)
                    self.is_running = False
                    self.undo_btn.config(state=tk.NORMAL)
                    self.load_record_btn.config(state=tk.NORMAL)
                    self.preview_btn.config(state=tk.NORMAL)
                    self.generate_preview_btn.config(state=tk.NORMAL)
                    self.log(f"撤销出错：{err_msg}", 'error')
                    messagebox.showerror("错误", f"撤销出错：{err_msg}")
                
                self.root.after(0, error_finish)
        
        thread = threading.Thread(target=run_undo)
        thread.daemon = True
        thread.start()
    
    def on_closing(self):
        """窗口关闭事件"""
        try:
            self.save_current_settings()
        except Exception as e:
            print(f"保存设置失败：{e}")
        self.root.destroy()


# ==================== 主程序入口 ====================
def main():
    root = tk.Tk()
    
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
    
    app = RenamerGUI(root)
    root.mainloop()


if __name__ == '__main__':
    if len(sys.argv) > 1:
        folder_path = sys.argv[1]
        renamer = FileRenamer()
        renamer.generate_preview([folder_path], {'custom_prefix': 'nb01.xyz', 'use_custom_prefix': True})
        renamed, errors, details = renamer.execute_rename()
        print(f"已重命名：{renamed}, 错误：{errors}")
    else:
        main()
