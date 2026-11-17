"""业务库备份装配器（r228 收归）：每仓仅声明配置面，行为单源 backup/backup_drill/wal_governance.

每仓 backup.py 由此收编为「shim = BackupShim(...) + 若干别名」，路径解析/多库前缀/
恢复演练/WAL 治理/CLI 的样板逻辑单点维护；仓内测试 monkeypatch shim 实例属性即可。
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Optional, Union

from nexus.backup import backup_cli, prune_backups, run_backup
from nexus.logging import get_logger

logger = get_logger(__name__)

DbSource = Union[Path, list[Path], Callable[[], Union[Path, list[Path]]]]
PrefixSource = Union[str, list[str], Callable[[], Union[str, list[str]]]]


class BackupShim:
    def __init__(
        self,
        db_paths: DbSource,
        prefix: PrefixSource,
        verify_tables: Union[str, list[str]],
        keep: int = 10,
        interval_hours: int = 6,
        drill_hours: Optional[int] = None,
        max_age_days: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
        keep_elder: Optional[int] = None,
        elder_interval_days: Optional[float] = None,
        cleanup_max_age_days: Optional[int] = None,
        cleanup_max_total_bytes: Optional[int] = None,
    ) -> None:
        self.db_paths = db_paths
        self.prefix = prefix
        self.verify_tables = verify_tables
        self.keep = keep
        self.interval_hours = interval_hours
        self.drill_hours = drill_hours
        self.max_age_days = max_age_days
        self.max_total_bytes = max_total_bytes
        self.keep_elder = keep_elder
        self.elder_interval_days = elder_interval_days
        self.cleanup_max_age_days = cleanup_max_age_days
        self.cleanup_max_total_bytes = cleanup_max_total_bytes

    def resolve_db_paths(self) -> Union[Path, list[Path]]:
        return self.db_paths() if callable(self.db_paths) else self.db_paths

    def resolve_prefix(self) -> Union[str, list[str]]:
        return self.prefix() if callable(self.prefix) else self.prefix

    def resolve_backup_dir(self) -> Path:
        dbs = self.resolve_db_paths()
        first = dbs[0] if isinstance(dbs, list) else dbs
        return first.parent / "backups"

    def _first_prefix(self) -> str:
        prefix = self.resolve_prefix()
        return prefix[0] if isinstance(prefix, list) else str(prefix)

    def create_backup(self) -> dict[str, object]:
        return run_backup(
            self.resolve_db_paths(),
            self.resolve_backup_dir(),
            self.resolve_prefix(),
            self.verify_tables,
            keep=self.keep,
            max_age_days=self.max_age_days,
            max_total_bytes=self.max_total_bytes,
            keep_elder=self.keep_elder,
            elder_interval_days=self.elder_interval_days,
        )

    def cleanup_old_backups(self, keep: Optional[int] = None) -> list[str]:
        return prune_backups(
            self.resolve_backup_dir(),
            self._first_prefix(),
            self.keep if keep is None else keep,
            self.cleanup_max_age_days,
            self.cleanup_max_total_bytes,
        )

    def register_backup(self, interval_hours: Optional[int] = None) -> Optional[object]:
        from nexus.backup import register_backup_job

        return register_backup_job(
            self.resolve_db_paths(),
            self.resolve_backup_dir(),
            self.resolve_prefix(),
            self.verify_tables,
            keep=self.keep,
            interval_hours=self.interval_hours if interval_hours is None else interval_hours,
            max_age_days=self.max_age_days,
            max_total_bytes=self.max_total_bytes,
            keep_elder=self.keep_elder,
            elder_interval_days=self.elder_interval_days,
        )

    def register_drill(self, interval_hours: Optional[int] = None) -> Optional[object]:
        from nexus.backup_drill import register_restore_drill_job

        dbs = self.resolve_db_paths()
        return register_restore_drill_job(
            self.resolve_backup_dir(),
            self._first_prefix(),
            source_db=dbs[0] if isinstance(dbs, list) else dbs,
            verify_tables=self.verify_tables,
            interval_hours=24 if interval_hours is None else interval_hours,
        )

    def register_wal_checkpoint(self, interval_hours: int = 6) -> Optional[object]:
        from nexus.wal_governance import register_wal_checkpoint_job

        return register_wal_checkpoint_job(self.resolve_db_paths(), interval_hours=interval_hours)

    def main(self, argv: Optional[list[str]] = None) -> int:
        return backup_cli(
            self.resolve_db_paths,
            self.resolve_backup_dir,
            self.resolve_prefix,
            self.verify_tables,
            keep=self.keep,
            interval_hours=self.interval_hours,
            argv=argv,
            max_age_days=self.max_age_days,
            max_total_bytes=self.max_total_bytes,
        )
