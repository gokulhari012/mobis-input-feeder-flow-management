"""Location of runtime data (database, logs, backups, exports)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "mobis.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def recovery_dir(self) -> Path:
        return self.data_dir / "recovery"

    @property
    def pending_restore_file(self) -> Path:
        return self.data_dir / "pending_restore.db"

    def ensure(self) -> None:
        for directory in (self.data_dir, self.logs_dir, self.backups_dir, self.exports_dir, self.recovery_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _writable(directory: Path) -> bool:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".write_test_", delete=True):
                pass
            return True
        except OSError:
            return False

    @classmethod
    def resolve(cls, data_dir: Optional[str] = None) -> "AppPaths":
        """Resolution order: ``--data-dir`` > ``MOBIS_DATA_DIR`` > ProgramData > user profile."""
        candidates: list[Path] = []
        if data_dir:
            candidates.append(Path(data_dir))
        if os.environ.get("MOBIS_DATA_DIR"):
            candidates.append(Path(os.environ["MOBIS_DATA_DIR"]))
        if not candidates:
            if os.name == "nt" and os.environ.get("PROGRAMDATA"):
                candidates.append(Path(os.environ["PROGRAMDATA"]) / "MOBIS")
            if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
                candidates.append(Path(os.environ["LOCALAPPDATA"]) / "MOBIS")
            candidates.append(Path.home() / ".mobis")
        for candidate in candidates:
            candidate = candidate.expanduser().resolve()
            if cls._writable(candidate):
                paths = cls(candidate)
                paths.ensure()
                return paths
        raise PermissionError(f"No writable data directory found (tried: {', '.join(map(str, candidates))})")
