"""Async filesystem watcher for LM Studio state.

Complements the API-driven eval path by observing:
  - ~/.cache/lm-studio/conversations/*/  (structured per-turn traces)
  - ~/.cache/lm-studio/server-logs/*/*.log (plugin lifecycle + tool activity)

Two modes:
  - Standalone CLI (python -m evals.harness.watcher): prints live events.
  - Library: subscribe to events programmatically; useful for observation-mode
    evaluation (user chats normally, watcher scores in the background).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer


@dataclass
class WatchEvent:
    kind: str  # 'conversation' | 'log'
    path: Path
    detail: str = ""


def default_paths() -> tuple[Path, Path]:
    cache = Path.home() / ".cache" / "lm-studio"
    return cache / "conversations", cache / "server-logs"


class _ConversationHandler(FileSystemEventHandler):
    def __init__(self, sink: Callable[[WatchEvent], None]) -> None:
        self.sink = sink
        self._sizes: dict[str, int] = {}

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix != ".json":
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        messages = data.get("messages", []) or []
        turn_count = len(messages)
        prev = self._sizes.get(str(p), -1)
        if turn_count != prev:
            self._sizes[str(p)] = turn_count
            self.sink(WatchEvent(kind="conversation", path=p, detail=f"{turn_count} messages"))


class _LogHandler(FileSystemEventHandler):
    """Tails the daily LM Studio log for cog-sandbox plugin lines."""

    def __init__(self, sink: Callable[[WatchEvent], None]) -> None:
        self.sink = sink
        self._offsets: dict[str, int] = {}

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix != ".log":
            return
        offset = self._offsets.get(str(p), 0)
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                new = f.read()
                self._offsets[str(p)] = f.tell()
        except FileNotFoundError:
            return
        for line in new.splitlines():
            if "cog-sandbox" in line:
                self.sink(WatchEvent(kind="log", path=p, detail=line.strip()))


def watch(conversations_dir: Path, logs_dir: Path, sink: Callable[[WatchEvent], None]) -> Observer:
    """Start a watchdog Observer wiring both directories to sink. Returns the observer;
    caller is responsible for .stop() and .join().
    """
    observer = Observer()
    if conversations_dir.exists():
        observer.schedule(_ConversationHandler(sink), str(conversations_dir), recursive=True)
    if logs_dir.exists():
        observer.schedule(_LogHandler(sink), str(logs_dir), recursive=True)
    observer.start()
    return observer


def _cli() -> None:
    conv, logs = default_paths()
    print(f"watching: {conv}")
    print(f"watching: {logs}")
    print("(ctrl-c to stop)")
    def pr(ev: WatchEvent) -> None:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {ev.kind:13s} {ev.detail[:200] if ev.detail else ev.path.name}")
    observer = watch(conv, logs, pr)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    _cli()
