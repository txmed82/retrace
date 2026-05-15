from __future__ import annotations
import sqlite3
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..core import Storage

class BaseRepository:
    def __init__(self, storage: "Storage"):
        self._storage = storage
    def _conn(self) -> sqlite3.Connection:
        return self._storage._conn()
