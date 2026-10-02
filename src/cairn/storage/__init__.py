"""Storage backends shared by the journal, memory and work queue."""

from cairn.storage.sqlite import SQLiteDatabase

__all__ = ["SQLiteDatabase"]
