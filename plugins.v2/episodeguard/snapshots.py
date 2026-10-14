"""Attach task words atomically to new native download history records."""

from copy import deepcopy
from threading import RLock
import time

from .overrides import MARKER, OverrideUnavailable, compatible_row


PLAN_TTL = 24 * 60 * 60
PLAN_LIMIT = 128


class TaskSnapshots:
    """Use an ORM insert hook so committed history never awaits a broadcast event."""

    def __init__(self, plans, save):
        self._lock = RLock()
        self._save = save
        self._plans = [p for p in (plans or []) if isinstance(p, dict) and p.get("expires", 0) > time.time()][:PLAN_LIMIT]
        self._model = None
        self._event = None
        self._reader = None

    def start(self, model=None, reader=None):
        """Register a removable SQLAlchemy hook; never replace host functions."""
        from sqlalchemy import event
        if model is None:
            from app.db.models.downloadhistory import DownloadHistory
            model = DownloadHistory
        self._model = model
        self._reader = reader
        self._event = event
        event.listen(model, "before_insert", self._before_insert)

    def stop(self):
        """Remove the exact listener installed by this instance."""
        if self._event is not None:
            self._event.remove(self._model, "before_insert", self._before_insert)
            self._event = None

    def rows(self, hashes):
        """Read only records for the prepared resource hashes using MP sessions."""
        if self._reader:
            return self._reader(hashes)
        from app.db import db_query
        model = self._model

        @db_query
        def read(db=None, values=None):
            records = db.query(model).filter(model.download_hash.in_(values)).all()
            return [{c.name: getattr(row, c.name) for c in model.__table__.columns} for row in records]

        return read(db=None, values=hashes)

    def prepare(self, plan):
        """Persist a validated plan before allowing the native download call."""
        for row in self.rows(plan["hashes"]):
            if not compatible_row(row, plan):
                raise OverrideUnavailable("同一哈希已关联其他媒体或季集，沿用模式不覆盖冲突记录")
        plan = deepcopy(plan)
        plan["expires"] = time.time() + PLAN_TTL
        with self._lock:
            current = [p for p in self._plans if p["expires"] > time.time()]
            for old in current:
                if set(old["hashes"]) & set(plan["hashes"]) and (
                        old["identity"] != plan["identity"] or old["target"] != plan["target"]):
                    raise OverrideUnavailable("同一哈希存在不同的待处理季集映射")
            current = [p for p in current if not (
                p["hashes"] == plan["hashes"] and p["origin"] == plan["origin"])]
            new = [plan, *current][:PLAN_LIMIT]
            self._save(new)
            self._plans = new

    def matching_file(self, name):
        """Return plans only for exact known file basenames."""
        with self._lock:
            return [deepcopy(p) for p in self._plans
                    if p["expires"] > time.time() and name in p["names"]]

    def matching_hash(self, value):
        with self._lock:
            return [deepcopy(p) for p in self._plans
                    if p["expires"] > time.time() and value.lower() in p["hashes"]]

    def _before_insert(self, _mapper, connection, target):
        """Words and marker are part of the same transaction as native history."""
        row = {c.name: getattr(target, c.name, None) for c in self._model.__table__.columns}
        with self._lock:
            plans = [p for p in self._plans if p["expires"] > time.time()
                     and str(row.get("download_hash", "")).lower() in p["hashes"]]
        source = (row.get("note") or {}).get("source") if isinstance(row.get("note"), dict) else None
        plans = [p for p in plans if p["origin"] == source and compatible_row(row, p)
                 and row.get("custom_words") == p["original_custom_words"]]
        if len(plans) != 1:
            return
        plan = plans[0]
        # Reject a conflicting concurrent insertion using this transaction's connection.
        from sqlalchemy import select
        table = self._model.__table__
        existing = connection.execute(select(table).where(table.c.download_hash.in_(plan["hashes"]))).mappings()
        if any(not compatible_row(dict(old), plan) for old in existing):
            return
        note = deepcopy(row.get("note") or {})
        note[MARKER] = {
            "version": 1, "identity": plan["identity"], "target": plan["target"],
            "files": plan["files"], "names": plan["names"],
            "original_custom_words": row.get("custom_words"),
        }
        target.custom_words = plan["words"]
        target.note = note

    def ready(self, hashes):
        """Require a unique frozen target; preserve the native latest-row preference."""
        rows = self.rows(hashes)
        marked = [r for r in rows if isinstance(r.get("note"), dict) and r["note"].get(MARKER)]
        if not marked or any(not compatible_row(r, {
                "identity": marked[0]["note"][MARKER]["identity"],
                "target": marked[0]["note"][MARKER]["target"]}) for r in rows):
            return None
        # Only use the latest record that MP itself would use for this hash.
        latest = max(rows, key=lambda r: (r.get("date") or "", r.get("id") or 0))
        if not isinstance(latest.get("note"), dict) or not latest["note"].get(MARKER):
            return None
        return latest
