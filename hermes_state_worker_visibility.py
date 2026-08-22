"""Explicit, receipt-backed migration for historical worker visibility.

This module is deliberately not wired into SessionDB startup. Operators and
tests must name a database explicitly; live profile databases are never swept
as a side effect of importing or upgrading Hermes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from hermes_state import is_owner_managed_session_source


RECEIPT_SCHEMA = "worker-session-visibility/v1"


def _delegate_marker(model_config: Optional[str]) -> bool:
    if not model_config:
        return False
    try:
        parsed = json.loads(model_config)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and bool(parsed.get("_delegate_from"))


def _database_path(db) -> str:
    return str(Path(db.db_path).resolve())


def _rollback(db, receipt: Mapping[str, Any]) -> dict[str, Any]:
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise ValueError("unsupported worker-session visibility receipt")
    if str(receipt.get("db_path") or "") != _database_path(db):
        raise ValueError("receipt belongs to a different database")

    changes = list(receipt.get("changes") or [])

    def _do(conn):
        restored: list[str] = []
        conflicts: list[str] = []
        for change in changes:
            sid = str(change.get("id") or "")
            previous = int(bool(change.get("hidden")))
            row = conn.execute(
                "SELECT hidden FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if row is None or int(row[0]) != 1:
                conflicts.append(sid)
                continue
            conn.execute(
                "UPDATE sessions SET hidden = ? WHERE id = ?", (previous, sid)
            )
            restored.append(sid)
        return restored, conflicts

    restored, conflicts = db._execute_write(_do)
    return {
        "mode": "rollback",
        "candidate_ids": [str(change.get("id") or "") for change in changes],
        "changed_ids": restored,
        "legacy_orphan_ids": [],
        "conflict_ids": conflicts,
        "receipt": None,
    }


def migrate_worker_session_visibility(
    db,
    *,
    workspaces_roots: Iterable[str | Path] = (),
    dry_run: bool = True,
    rollback_receipt: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Hide positively identified historical workers or roll back one receipt.

    Direct worker sources and stable ``_delegate_from`` markers are candidates.
    ``workspaces_roots`` remains accepted for call compatibility but cwd alone
    is never treated as ownership evidence. Parentless child rows without an
    authoritative marker are reported as legacy orphans, never guessed.
    """
    if rollback_receipt is not None:
        return _rollback(db, rollback_receipt)

    _ = workspaces_roots
    with db._read_ctx() as conn:
        rows = conn.execute(
            "SELECT id, source, hidden, cwd, model_config, parent_session_id "
            "FROM sessions ORDER BY id"
        ).fetchall()
        known_ids = {row["id"] for row in rows}

    candidate_ids: list[str] = []
    legacy_orphan_ids: list[str] = []
    prior_hidden: dict[str, int] = {}
    for row in rows:
        source = str(row["source"] or "").strip().lower()
        candidate = (
            is_owner_managed_session_source(source)
            or _delegate_marker(row["model_config"])
        )
        if candidate:
            candidate_ids.append(row["id"])
            prior_hidden[row["id"]] = int(row["hidden"] or 0)
        elif row["parent_session_id"] and row["parent_session_id"] not in known_ids:
            legacy_orphan_ids.append(row["id"])

    change_ids = [sid for sid in candidate_ids if prior_hidden[sid] == 0]
    if dry_run:
        return {
            "mode": "dry-run",
            "candidate_ids": candidate_ids,
            "changed_ids": [],
            "would_change_ids": change_ids,
            "legacy_orphan_ids": legacy_orphan_ids,
            "conflict_ids": [],
            "receipt": None,
        }

    def _do(conn):
        changed: list[str] = []
        for sid in change_ids:
            cursor = conn.execute(
                "UPDATE sessions SET hidden = 1 WHERE id = ? AND hidden = 0", (sid,)
            )
            if cursor.rowcount:
                changed.append(sid)
        return changed

    changed_ids = db._execute_write(_do)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "db_path": _database_path(db),
        "created_at": time.time(),
        "changes": [
            {"id": sid, "hidden": prior_hidden[sid]} for sid in changed_ids
        ],
    }
    return {
        "mode": "apply",
        "candidate_ids": candidate_ids,
        "changed_ids": changed_ids,
        "legacy_orphan_ids": legacy_orphan_ids,
        "conflict_ids": [],
        "receipt": receipt,
    }
