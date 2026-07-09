"""backup_milvus_data.py — 运维脚本：文件级全量备份/恢复 Milvus 数据目录。

原理：Milvus 2.6 standalone 不支持 Snapshot API，数据存储在 GPFS 上的
``milvus-data/`` 目录（etcd、storage、data、minio_data 等）。备份就是拷贝这个目录。

关键：**必须停止 Milvus 后再备份**，否则后台写入可能导致数据不一致。

用法::

    python scripts/backup_milvus_data.py backup         # 备份到 backup_dir
    python scripts/backup_milvus_data.py backup --to /path/to/backup  # 指定目标
    python scripts/backup_milvus_data.py restore /path/to/backup   # 恢复
    python scripts/backup_milvus_data.py status         # 检查备份时间戳

与 ``compass store backup`` 的区别：
  - 文件级：完整快照，用于灾难恢复，必须停服
  - 逻辑级：在线导出 JSONL，可选择性恢复，无需停服
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# Default paths — overridable via environment variables for portability.
_GPFS_MILVUS_DATA = Path(
    os.environ.get(
        "COMPASS_MILVUS_DATA_DIR",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/milvus-data",
    )
)
_DEFAULT_BACKUP_ROOT = Path(
    os.environ.get(
        "COMPASS_MILVUS_BACKUP_DIR",
        "/inspire/qb-ilm/project/multi-agent/niexiaohang-25130061/academic-data/milvus-backups",
    )
)
_MILVUS_PORT = int(os.environ.get("COMPASS_MILVUS_PORT", "19530"))


def _milvus_ip() -> str | None:
    ip_file = _GPFS_MILVUS_DATA / "milvus_ip.txt"
    if ip_file.exists():
        return ip_file.read_text().strip()
    return None


def _milvus_running() -> bool:
    """Check whether the Milvus gRPC port is accepting connections."""
    ip = _milvus_ip()
    if not ip:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        try:
            s.connect((ip, _MILVUS_PORT))
            return True
        except OSError:
            return False


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy *src* directory tree to *dst*, preferring rsync when available."""
    if shutil.which("rsync"):
        subprocess.run(
            ["rsync", "-a", "--progress", str(src) + "/", str(dst) + "/"],
            check=True,
        )
    else:
        shutil.copytree(src, dst, dirs_exist_ok=False)


def cmd_backup(args: argparse.Namespace) -> None:
    backup_root = Path(args.to) if args.to else _DEFAULT_BACKUP_ROOT
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_root / f"milvus_{ts}"

    # ── Safety check ──
    if _milvus_running():
        print(
            "⚠  Milvus is still running. Backup may be inconsistent.\n"
            "   Stop the Milvus container first, then re-run:\n"
            "     docker stop <milvus-container>"
        )
        if not args.force:
            print("   Use --force to backup anyway (not recommended).")
            sys.exit(1)

    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"Backing up {_GPFS_MILVUS_DATA} → {dest}")
    t0 = time.perf_counter()
    _copy_tree(_GPFS_MILVUS_DATA, dest)

    elapsed = time.perf_counter() - t0
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"Backup complete: {dest}")
    print(f"  Size: {size / (1024**3):.1f} GB")
    print(f"  Time: {elapsed:.0f}s")

    # Write metadata
    meta = dest / "backup_meta.txt"
    meta.write_text(
        f"timestamp: {dt.datetime.now().isoformat()}\n"
        f"source: {_GPFS_MILVUS_DATA}\n"
        f"milvus_ip: {_milvus_ip() or 'N/A'}\n"
        f"running: {_milvus_running()}\n"
    )


def cmd_restore(args: argparse.Namespace) -> None:
    source = Path(args.source)
    if not (source / "etcd").exists() or not (source / "storage").exists():
        print(f"Error: {source} doesn't look like a valid Milvus data backup")
        sys.exit(1)

    meta = source / "backup_meta.txt"
    if meta.exists():
        print(f"Backup metadata: {meta.read_text().strip()}")

    if _milvus_running():
        print("⚠  Milvus is running — stop it first before restore.")
        sys.exit(1)

    if not args.yes:
        try:
            resp = input(
                f"Move current {_GPFS_MILVUS_DATA} aside and restore from {source}? [y/N] "
            )
        except EOFError:
            print("Aborted: non-interactive mode requires --yes")
            sys.exit(1)
        if resp.lower() not in ("y", "yes"):
            print("Aborted.")
            return

    # Move current aside, then copy backup in.  If copy fails, roll back.
    old = Path(str(_GPFS_MILVUS_DATA) + ".old." + dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    print(f"Moving current → {old}")
    shutil.move(str(_GPFS_MILVUS_DATA), str(old))

    print(f"Copying backup → {_GPFS_MILVUS_DATA}")
    t0 = time.perf_counter()
    try:
        _copy_tree(source, _GPFS_MILVUS_DATA)
    except Exception:
        print("Restore FAILED — rolling back to original data.")
        if _GPFS_MILVUS_DATA.exists():
            shutil.rmtree(_GPFS_MILVUS_DATA, ignore_errors=True)
        shutil.move(str(old), str(_GPFS_MILVUS_DATA))
        print(f"Original data restored from {old}")
        sys.exit(1)

    elapsed = time.perf_counter() - t0
    print(f"Restore complete in {elapsed:.0f}s")
    print(f"Previous data preserved at: {old}")
    print("Start Milvus container now.")


def cmd_status(args: argparse.Namespace) -> None:
    backup_root = Path(args.dir) if args.dir else _DEFAULT_BACKUP_ROOT
    if not backup_root.exists():
        print(f"No backups found at {backup_root}")
        return

    backups = sorted(
        [d for d in backup_root.iterdir() if d.is_dir() and d.name.startswith("milvus_")],
        reverse=True,
    )
    print(f"{'#':4s}  {'Timestamp':20s}  {'Size':>10s}  {'Path'}")
    print("-" * 70)
    for i, d in enumerate(backups, 1):
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        ts = d.name.replace("milvus_", "")
        print(f"{i:4d}  {ts:20s}  {size / (1024**3):8.1f}G  {d}")

    print(f"\nMilvus running: {_milvus_running()}")
    print(f"IP: {_milvus_ip() or 'N/A'}")


def main() -> None:
    p = argparse.ArgumentParser(description="File-level Milvus data backup/restore (GPFS)")
    sp = p.add_subparsers(dest="command", required=True)

    sp_backup = sp.add_parser("backup", help="Copy entire milvus-data directory")
    sp_backup.add_argument("--to", help="Target directory")
    sp_backup.add_argument("--force", action="store_true", help="Backup even if Milvus running")

    sp_restore = sp.add_parser("restore", help="Restore from a backup directory")
    sp_restore.add_argument("source", help="Path to backup directory")
    sp_restore.add_argument("--yes", action="store_true", help="Skip confirmation")

    sp_status = sp.add_parser("status", help="List backups")
    sp_status.add_argument("--dir", help=f"Backup directory (default: {_DEFAULT_BACKUP_ROOT})")

    args = p.parse_args()
    if args.command == "backup":
        cmd_backup(args)
    elif args.command == "restore":
        cmd_restore(args)
    else:
        cmd_status(args)


if __name__ == "__main__":
    main()
