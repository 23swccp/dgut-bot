"""Service keepalive ownership; explicit shutdown always overrides leases."""

from contextlib import contextmanager
import threading
from uuid import uuid4


class LeaseManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str], bool] = {}

    def set(self, kind: str, owner: str, active: bool) -> None:
        with self._lock:
            if active:
                self._leases[(kind, owner)] = True
            else:
                self._leases.pop((kind, owner), None)

    @contextmanager
    def hold(self, kind: str):
        owner = uuid4().hex
        self.set(kind, owner, True)
        try:
            yield
        finally:
            self.set(kind, owner, False)

    def active(self) -> bool:
        with self._lock:
            return bool(self._leases)

    def snapshot(self) -> list[str]:
        with self._lock:
            return sorted({kind for kind, _owner in self._leases})
