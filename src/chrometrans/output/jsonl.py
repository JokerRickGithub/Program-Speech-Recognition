"""JSONL 权威记录。每 cue 一行，append + flush + fsync（spec §5.5）。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from chrometrans.models import Cue


def session_dir(root: Path, when: datetime) -> Path:
    """按时间戳建会话目录，如 transcripts/2026-09-22_1420/。"""
    path = root / when.strftime("%Y-%m-%d_%H%M")
    path.mkdir(parents=True, exist_ok=True)
    return path


class JsonlWriter:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._fh = None

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a", encoding="utf-8")

    def append(self, cue: Cue) -> None:
        if self._fh is None:
            raise RuntimeError("JsonlWriter 未 open()")
        line = json.dumps(cue.to_dict(), ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def read_all(self) -> list[Cue]:
        if not self._path.exists():
            return []
        cues = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    cues.append(Cue.from_dict(json.loads(line)))
        return cues

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
