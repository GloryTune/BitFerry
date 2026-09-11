"""Daily diagnostic logs and serialized background persistence."""
import json
import queue
import re
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path


class DailyLog:
    def __init__(self, root):
        self.root = Path(root)
        self.closed = False
        self.recent = deque(maxlen=1000)
        self.queue = queue.Queue()
        self._last_cleanup = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def record(self, level, event):
        if self.closed:
            return
        event = re.sub(r'(?i)([?&](?:k|token|key)=)[^&\s]+', r'\1[redacted]', str(event))
        now = datetime.now()
        line = f'{now:%Y-%m-%d %H:%M:%S.%f} [{level.upper()}] {str(event).replace(chr(10), " | ")}'
        self.recent.append(line)
        self.queue.put((self.root, now, line))

    def _write(self, root, now, line):
        folder = root / 'log'
        folder.mkdir(parents=True, exist_ok=True)
        key = (str(folder), now.date())
        if key != self._last_cleanup:
            cutoff = now.date() - timedelta(days=6)
            for file in folder.glob('????-??-??.log'):
                try:
                    day = datetime.strptime(file.stem, '%Y-%m-%d').date()
                    if day < cutoff and file.is_file() and not file.is_symlink():
                        file.unlink()
                except (ValueError, OSError):
                    pass
            self._last_cleanup = key
        with (folder / f'{now:%Y-%m-%d}.log').open('a', encoding='utf-8') as out:
            out.write(line + '\n')

    def _run(self):
        while True:
            try:
                item = self.queue.get(timeout=60)
            except queue.Empty:
                now = datetime.now()
                if self._last_cleanup != (str(self.root / 'log'), now.date()):
                    try:
                        self._write(self.root, now, f"{now:%Y-%m-%d %H:%M:%S} [INFO] 日志日切与保留期检查")
                    except OSError:
                        pass
                continue
            try:
                if item is None:
                    return
                self._write(*item)
            except Exception as exc:
                self.recent.append(f'[ERROR] 日志保存失败：{exc}')
            finally:
                self.queue.task_done()

    def flush(self):
        self.queue.join()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.queue.put(None)
        self.queue.join()


class SerialWriter:
    """The UI passes a stable snapshot; serialization/fsync happens on one worker."""
    def __init__(self, write, on_error):
        self.write, self.on_error = write, on_error
        self.closed = False
        self.queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, path, value):
        if self.closed:
            return
        self.queue.put((path, value))

    def _run(self):
        while True:
            task = self.queue.get()
            try:
                if task is None:
                    return
                if not self.write(*task):
                    self.on_error('草稿保存失败，请检查磁盘空间和目录权限')
            except Exception as exc:
                self.on_error(str(exc))
            finally:
                self.queue.task_done()

    def flush(self):
        self.queue.join()

    def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put(None)
            self.queue.join()


def collect_paths(value):
    paths = set()
    def walk(item):
        if isinstance(item, dict):
            for key, val in item.items():
                if key in ('path', 'final_dir') and isinstance(val, str):
                    paths.add(str(Path(val).resolve()))
                elif key == 'payload' and isinstance(val, str):
                    try:
                        walk(json.loads(val))
                    except (ValueError, TypeError):
                        pass
                else:
                    walk(val)
        elif isinstance(item, (list, tuple)):
            for val in item:
                walk(val)
        elif isinstance(item, str) and Path(item).is_absolute():
            paths.add(str(Path(item).resolve()))
    walk(value)
    return paths


def cache_inventory(image_root, temp_root, references, now=None):
    now = datetime.now().timestamp() if now is None else now
    protected = set(references)
    for path in list(protected):
        protected.add(str(Path(path).with_suffix('.base.png')))
        protected.add(str(Path(path).with_suffix('.layers.json')))
    total = 0
    candidates = []
    for root in (Path(image_root), Path(temp_root)):
        if not root.exists():
            continue
        for file in root.rglob('*'):
            if file.is_symlink() or not file.is_file():
                continue
            resolved = file.resolve()
            if root.resolve() not in resolved.parents:
                continue
            stat = file.stat()
            total += stat.st_size
            if str(resolved) not in protected and not any(str(parent) in protected for parent in resolved.parents) and now - stat.st_mtime >= 24 * 3600:
                candidates.append((str(file), stat.st_size, stat.st_mtime_ns))
    return total, candidates


def delete_cache_candidates(candidates):
    removed = 0
    for name, size, modified in candidates:
        file = Path(name)
        try:
            if file.is_symlink():
                continue
            stat = file.stat()
            if (stat.st_size, stat.st_mtime_ns) != (size, modified):
                continue
            file.unlink()
            removed += size
        except FileNotFoundError:
            pass
    return removed
