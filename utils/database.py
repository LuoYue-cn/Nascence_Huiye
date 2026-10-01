"""Nested core commits participate in one explicit transaction."""
import sqlite3

class CoreConnection(sqlite3.Connection):
    atomic_depth = 0

    def commit(self):
        if not self.atomic_depth:
            super().commit()
