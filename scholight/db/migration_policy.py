"""Production migration integrity and expand-only policy checks."""

from __future__ import annotations

import hashlib
import re

_CONTRACT_MARKER = "scholight: migration-phase=contract"
_DESTRUCTIVE_SQL = re.compile(
    r"\b(?:"
    r"TRUNCATE(?:\s+TABLE)?|"
    r"DROP\s+(?:TABLE|COLUMN|TYPE|SCHEMA|INDEX|CONSTRAINT|VIEW|DATABASE)|"
    r"ALTER\s+TABLE\b[^;]*\bRENAME\b|"
    r"ALTER\s+TABLE\b[^;]*\bALTER\s+COLUMN\b[^;]*\bTYPE\b|"
    r"ALTER\s+TYPE\b[^;]*\bRENAME\b"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_SINGLE_QUOTED = re.compile(r"'(?:''|[^'])*'")


def migration_checksum(sql: str) -> str:
    """Return the stable SHA-256 digest for one migration file's exact contents."""
    return hashlib.sha256(sql.encode()).hexdigest()


def validate_expand_only_sql(
    sql: str,
    *,
    allow_contract: bool = False,
    approved_destructive_checksums: frozenset[str] = frozenset(),
) -> None:
    """Reject destructive pending SQL unless explicitly approved for a contract release."""
    checksum = migration_checksum(sql)
    if checksum in approved_destructive_checksums:
        return

    executable = _BLOCK_COMMENT.sub(" ", sql)
    executable = _LINE_COMMENT.sub(" ", executable)
    executable = _SINGLE_QUOTED.sub("''", executable)
    if not _DESTRUCTIVE_SQL.search(executable):
        return
    if allow_contract and _CONTRACT_MARKER in sql.lower():
        return

    msg = (
        "destructive migration rejected by expand-only policy; "
        "run contract migrations in a separately reviewed maintenance release"
    )
    raise ValueError(msg)
