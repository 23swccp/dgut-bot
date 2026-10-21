"""本机作业归档：按登录账号保存课程与单条作业的归档集合。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


ARCHIVE_VERSION = 1
ARCHIVE_FILE = "homework-archive.json"


def _clean_ids(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = {
        str(value).strip()
        for value in values
        if isinstance(value, (str, int)) and str(value).strip()
    }
    return sorted(cleaned)


class HomeworkArchive:
    """只在本机保存归档标记，不向平台写入任何状态。"""

    def __init__(self, root: Path) -> None:
        self.path = root / ARCHIVE_FILE
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, list[str]]] = self._load()

    def _load(self) -> dict[str, dict[str, list[str]]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        users = data.get("users") if isinstance(data, dict) else None
        if not isinstance(users, dict):
            return {}
        normalized: dict[str, dict[str, list[str]]] = {}
        for key, entry in users.items():
            if not isinstance(entry, dict):
                continue
            normalized[str(key)] = {
                "courses": _clean_ids(entry.get("courses")),
                "items": _clean_ids(entry.get("items")),
            }
        return normalized

    @staticmethod
    def user_key(user_id: object) -> str:
        text = str(user_id or "").strip()
        return text or "anonymous"

    def sets(self, user_id: object) -> tuple[set[str], set[str]]:
        with self._lock:
            entry = self._users.get(self.user_key(user_id)) or {}
            return set(entry.get("courses", [])), set(entry.get("items", []))

    def set_archived(self, user_id: object, kind: str, target_id: object, archived: bool) -> tuple[set[str], set[str]]:
        if kind not in {"course", "item"}:
            raise ValueError("归档类型无效")
        target = str(target_id or "").strip()
        if not target or len(target) > 200:
            raise ValueError("归档对象标识无效")
        with self._lock:
            key = self.user_key(user_id)
            entry = self._users.setdefault(key, {"courses": [], "items": []})
            field = "courses" if kind == "course" else "items"
            values = set(entry.get(field, []))
            if archived:
                values.add(target)
            else:
                values.discard(target)
            entry[field] = sorted(values)
            self._write()
            return set(entry.get("courses", [])), set(entry.get("items", []))

    def _write(self) -> None:
        payload = {"version": ARCHIVE_VERSION, "users": self._users}
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
