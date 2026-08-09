"""SQLite storage. Applies contracts/db/schema.sql and nothing else.

The DDL carries the invariants (idempotency uniqueness, atomic supersession, the
signed-approval requirement). This layer must never work around a constraint it
trips - a constraint firing means the caller is wrong.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .approval import rfc3339, utcnow
from .embedding import serialize
from .errors import IdempotencyConflict, StoreBusy, VectorUnavailable

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "db" / "schema.sql"

# Applied only when sqlite-vec loads. Kept here rather than in schema.sql because
# schema.sql must stay runnable on a stock SQLite build (see the VECTOR INDEX
# marker in that file).
VEC_DDL = "CREATE VIRTUAL TABLE IF NOT EXISTS records_vec USING vec0(embedding float[{dim}])"


def new_id() -> str:
    return str(uuid.uuid4())


class Store:
    def __init__(self, path: str, *, dimensions: int = 384, busy_timeout_ms: int = 5000,
                 require_vector: bool = False):
        self.path = path
        self.dimensions = dimensions
        self.vector_ok = False
        self.vector_error: str | None = None

        self.con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self.con.executescript(SCHEMA_PATH.read_text())
        self.con.execute("PRAGMA foreign_keys = ON")
        self._load_vector(require_vector)

    # -- lifecycle ---------------------------------------------------------
    def _load_vector(self, require: bool) -> None:
        try:
            import sqlite_vec  # noqa: PLC0415

            self.con.enable_load_extension(True)
            sqlite_vec.load(self.con)
            self.con.enable_load_extension(False)
            self.con.execute(VEC_DDL.format(dim=self.dimensions))
            self.vector_ok = True
        except Exception as exc:
            self.vector_error = f"{type(exc).__name__}: {exc}"
            if require:
                raise VectorUnavailable(
                    f"sqlite-vec could not be loaded and require_vector_extension is true: {self.vector_error}"
                ) from exc

    def close(self) -> None:
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- helpers -----------------------------------------------------------
    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        try:
            return self.con.execute(sql, args).fetchall()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise StoreBusy(str(exc)) from exc
            raise

    def meta(self, key: str, default: str = "") -> str:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self._q("INSERT INTO meta (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- records -----------------------------------------------------------
    def write_record(
        self, *, rtype: str, scope: str, content: str, payload: dict | None = None,
        tags: list[str] | None = None, importance: float = 0.5, confidence: float = 0.5,
        provenance: dict | None = None, expires_at: str | None = None,
        idempotency_key: str | None = None, embedding: list[float] | None = None,
        embedding_model: str | None = None, attrs: dict | None = None, now: str | None = None,
    ) -> tuple[str, bool]:
        """Returns (record_id, created). created=False means an idempotency key
        matched and the original record was returned untouched."""
        now = now or rfc3339(utcnow())
        if idempotency_key is not None:
            existing = self._q(
                "SELECT id, content FROM records WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key))
            if existing:
                # Same key, different content is a caller bug worth surfacing:
                # silently returning the old record would hide a real collision.
                if existing[0]["content"] != content:
                    raise IdempotencyConflict(
                        "idempotency key reused with different content",
                        record_id=existing[0]["id"], scope=scope, idempotency_key=idempotency_key)
                return existing[0]["id"], False

        rid = new_id()
        self._q("BEGIN")
        try:
            self._q(
                "INSERT INTO records (id,type,scope,content,payload,created_at,updated_at,"
                "last_accessed_at,importance,confidence,provenance,tags,expires_at,"
                "idempotency_key,embedding_model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, rtype, scope, content, json.dumps(payload) if payload else None, now, now,
                 now, importance, confidence,
                 json.dumps(provenance) if provenance else None,
                 json.dumps(tags) if tags else None, expires_at, idempotency_key, embedding_model))
            if attrs:
                self._insert_attrs(rid, rtype, attrs)
            if embedding is not None and self.vector_ok:
                seq = self._q("SELECT seq FROM records WHERE id=?", (rid,))[0]["seq"]
                self._q("INSERT INTO records_vec (rowid, embedding) VALUES (?,?)",
                        (seq, serialize(embedding)))
            self._q("COMMIT")
        except Exception:
            self._q("ROLLBACK")
            raise
        return rid, True

    def _insert_attrs(self, rid: str, rtype: str, a: dict) -> None:
        if rtype == "episodic":
            action = a.get("action") or {}
            self._q(
                "INSERT INTO episodic_attrs (record_id,session_id,cycle_id,parent_cycle_id,step_no,"
                "goal,action_class,action_name,action_input,observation,outcome,reward,error,duration_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, a["session_id"], a["cycle_id"], a.get("parent_cycle_id"), a["step_no"],
                 a.get("goal"), action.get("class"), action.get("name"),
                 json.dumps(action["input"]) if action.get("input") else None,
                 a.get("observation"), a.get("outcome", "unknown"), a.get("reward"),
                 json.dumps(a["error"]) if a.get("error") else None, a.get("duration_ms")))
        elif rtype == "semantic":
            self._q(
                "INSERT INTO semantic_attrs (record_id,subject,predicate,object,valid_from,valid_to,"
                "contradicts,corroborations,volatility,sensitivity) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, a.get("subject"), a.get("predicate"), a.get("object"), a.get("valid_from"),
                 a.get("valid_to"), json.dumps(a.get("contradicts", [])),
                 a.get("corroborations", 1), a.get("volatility", "slow"), a.get("sensitivity", "none")))
        elif rtype == "procedural":
            sig = a["signature"]
            self._q(
                "INSERT INTO procedural_attrs (record_id,trigger_text,preconditions,steps,"
                "success_signal,failure_signal,approval_state,reviewed_by,reviewed_at,rationale,"
                "sig_alg,sig_key_id,sig_payload,sig_value,candidate_sha256,sig_verified_at,supersedes)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, a["trigger"], json.dumps(a.get("preconditions", [])), json.dumps(a["steps"]),
                 a.get("success_signal"), a.get("failure_signal"), a.get("approval_state", "approved"),
                 a["reviewed_by"], a["reviewed_at"], a.get("rationale"), sig.get("alg", "ed25519"),
                 sig["key_id"], sig["signed_payload"], sig["sig"], sig["candidate_sha256"],
                 a.get("sig_verified_at"), a.get("supersedes")))

    def get_record(self, rid: str) -> dict | None:
        rows = self._q("SELECT * FROM records WHERE id=?", (rid,))
        if not rows:
            return None
        rec = dict(rows[0])
        for table, key in (("episodic_attrs", "episodic"), ("semantic_attrs", "semantic"),
                           ("procedural_attrs", "procedural")):
            if rec["type"] == key:
                extra = self._q(f"SELECT * FROM {table} WHERE record_id=?", (rid,))
                if extra:
                    rec.update({k: v for k, v in dict(extra[0]).items() if k != "record_id"})
        return rec

    def supersede(self, old_id: str, new_id_: str) -> None:
        self._q("UPDATE records SET status='superseded', superseded_by=? WHERE id=?", (new_id_, old_id))

    def touch(self, ids: list[str], now: str | None = None) -> None:
        if not ids:
            return
        now = now or rfc3339(utcnow())
        self._q(
            f"UPDATE records SET last_accessed_at=?, access_count=access_count+1 "
            f"WHERE id IN ({','.join('?' * len(ids))})", (now, *ids))

    def count_scope(self, scope: str) -> int:
        return self._q("SELECT count(*) c FROM records WHERE scope=?", (scope,))[0]["c"]

    # -- search ------------------------------------------------------------
    def _filter_sql(self, scope: str, types: list[str], filters: dict) -> tuple[str, list]:
        sql = " AND r.scope=? AND r.type IN (%s)" % ",".join("?" * len(types))
        args: list = [scope, *types]
        if filters.get("min_confidence"):
            sql += " AND r.confidence >= ?"
            args.append(filters["min_confidence"])
        if filters.get("time_from"):
            sql += " AND r.created_at >= ?"
            args.append(filters["time_from"])
        if filters.get("time_to"):
            sql += " AND r.created_at <= ?"
            args.append(filters["time_to"])
        for tag in filters.get("tags") or []:
            sql += " AND EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value=?)"
            args.append(tag)
        if filters.get("session_id"):
            sql += (" AND (r.type != 'episodic' OR EXISTS (SELECT 1 FROM episodic_attrs e "
                    "WHERE e.record_id=r.id AND e.session_id=?))")
            args.append(filters["session_id"])
        # Unapproved procedures cannot appear here anyway - they live in
        # `proposals`, not `records` - but a rejected one could, so filter.
        sql += (" AND (r.type != 'procedural' OR EXISTS (SELECT 1 FROM procedural_attrs p "
                "WHERE p.record_id=r.id AND p.approval_state='approved'))")
        return sql, args

    def search_keyword(self, scope: str, query: str, types: list[str], limit: int,
                       filters: dict, include_superseded: bool = False) -> list[tuple[str, int]]:
        base = "SELECT r.id FROM records_fts f JOIN records r ON r.seq=f.rowid WHERE f.records_fts MATCH ?"
        if not include_superseded:
            base += (" AND r.status='active' AND (r.expires_at IS NULL OR r.expires_at > ?)")
        cond, args = self._filter_sql(scope, types, filters)
        head: list = [_fts_query(query)]
        if not include_superseded:
            head.append(rfc3339(utcnow()))
        try:
            rows = self._q(base + cond + " ORDER BY bm25(records_fts) LIMIT ?", (*head, *args, limit))
        except sqlite3.OperationalError:
            return []  # malformed FTS query -> no keyword hits, not a crash
        return [(r["id"], i + 1) for i, r in enumerate(rows)]

    def search_vector(self, scope: str, embedding: list[float], types: list[str], limit: int,
                      filters: dict, include_superseded: bool = False) -> list[tuple[str, int, float]]:
        if not self.vector_ok:
            return []
        # Over-fetch, then post-filter: vec0 KNN cannot express the scope/type
        # predicates, so filtering afterwards is what keeps recall correct.
        rows = self._q(
            "SELECT rowid, distance FROM records_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (serialize(embedding), max(limit * 5, 50)))
        if not rows:
            return []
        seqs = {r["rowid"]: r["distance"] for r in rows}
        base = "SELECT r.id, r.seq FROM records r WHERE r.seq IN (%s)" % ",".join("?" * len(seqs))
        args: list = list(seqs)
        if not include_superseded:
            base += " AND r.status='active' AND (r.expires_at IS NULL OR r.expires_at > ?)"
            args.append(rfc3339(utcnow()))
        cond, fargs = self._filter_sql(scope, types, filters)
        kept = self._q(base + cond, (*args, *fargs))
        ordered = sorted(kept, key=lambda r: seqs[r["seq"]])[:limit]
        return [(r["id"], i + 1, 1.0 - seqs[r["seq"]] / 2.0) for i, r in enumerate(ordered)]

    def recent(self, scope: str, types: list[str], limit: int, filters: dict) -> list[tuple[str, int]]:
        cond, args = self._filter_sql(scope, types, filters)
        rows = self._q(
            "SELECT r.id FROM records r WHERE r.status='active' "
            "AND (r.expires_at IS NULL OR r.expires_at > ?)" + cond +
            " ORDER BY r.created_at DESC LIMIT ?", (rfc3339(utcnow()), *args, limit))
        return [(r["id"], i + 1) for i, r in enumerate(rows)]

    # -- cycles ------------------------------------------------------------
    def open_cycle(self, *, session_id: str, scope: str, goal: str, loop_signature: str,
                   parent_cycle_id: str | None, ttl_hours: float, now: datetime | None = None) -> str:
        now = now or utcnow()
        cid = new_id()
        self._q(
            "INSERT INTO working_set (cycle_id,session_id,parent_cycle_id,scope,goal,loop_signature,"
            "status,opened_at,expires_at) VALUES (?,?,?,?,?,?,'open',?,?)",
            (cid, session_id, parent_cycle_id, scope, goal, loop_signature,
             rfc3339(now), rfc3339(now + timedelta(hours=ttl_hours))))
        return cid

    def get_cycle(self, cycle_id: str) -> dict | None:
        rows = self._q("SELECT * FROM working_set WHERE cycle_id=?", (cycle_id,))
        return dict(rows[0]) if rows else None

    def similar_cycles(self, scope: str, loop_signature: str, since: datetime, limit: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT cycle_id, outcome, status, opened_at FROM working_set "
            "WHERE scope=? AND loop_signature=? AND opened_at >= ? "
            "ORDER BY opened_at DESC LIMIT ?", (scope, loop_signature, rfc3339(since), limit))]

    def add_observation(self, cycle_id: str, step_no: int, text: str, now: str | None = None) -> None:
        self._q("INSERT OR REPLACE INTO observations (cycle_id,step_no,at,text) VALUES (?,?,?,?)",
                (cycle_id, step_no, now or rfc3339(utcnow()), text))

    def observations(self, cycle_id: str) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT step_no, at, text FROM observations WHERE cycle_id=? ORDER BY step_no", (cycle_id,))]

    def close_cycle(self, cycle_id: str, outcome: str, now: str | None = None,
                    status: str = "closed") -> None:
        self._q("UPDATE working_set SET status=?, outcome=?, closed_at=? WHERE cycle_id=?",
                (status, outcome, now or rfc3339(utcnow()), cycle_id))

    def set_retrieved(self, cycle_id: str, entries: list[dict]) -> None:
        self._q("UPDATE working_set SET retrieved=? WHERE cycle_id=?", (json.dumps(entries), cycle_id))

    def stale_cycles(self, now: datetime | None = None) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM working_set WHERE status='open' AND expires_at <= ?",
            (rfc3339(now or utcnow()),))]

    def writes_in_last_minute(self, session_id: str, now: datetime | None = None) -> int:
        since = rfc3339((now or utcnow()) - timedelta(minutes=1))
        return self._q(
            "SELECT count(*) c FROM episodic_attrs e JOIN records r ON r.id=e.record_id "
            "WHERE e.session_id=? AND r.created_at >= ?", (session_id, since))[0]["c"]

    # -- proposals ---------------------------------------------------------
    def create_proposal(self, *, scope: str, kind: str, candidate: dict, rationale: str,
                        proposed_by: str, source: str, dedupe_key: str | None,
                        now: str | None = None) -> tuple[str, bool]:
        if dedupe_key:
            existing = self._q(
                "SELECT id FROM proposals WHERE scope=? AND dedupe_key=? AND state='pending'",
                (scope, dedupe_key))
            if existing:
                return existing[0]["id"], True
        pid = "p_" + uuid.uuid4().hex[:16]
        self._q("INSERT INTO proposals (id,scope,kind,candidate,rationale,proposed_by,proposed_at,"
                "source,dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, scope, kind, json.dumps(candidate), rationale, proposed_by,
                 now or rfc3339(utcnow()), source, dedupe_key))
        return pid, False

    def get_proposal(self, pid: str) -> dict | None:
        rows = self._q("SELECT * FROM proposals WHERE id=?", (pid,))
        return dict(rows[0]) if rows else None

    def list_proposals(self, scope: str | None, state: str, kind: str | None, limit: int) -> list[dict]:
        sql, args = "SELECT * FROM proposals WHERE state=?", [state]
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return [dict(r) for r in self._q(sql + " ORDER BY proposed_at LIMIT ?", (*args, limit))]

    def queue_depth(self, scope: str | None) -> int:
        if scope:
            return self._q("SELECT count(*) c FROM proposals WHERE state='pending' AND scope=?",
                           (scope,))[0]["c"]
        return self._q("SELECT count(*) c FROM proposals WHERE state='pending'")[0]["c"]

    def decide_proposal(self, pid: str, *, state: str, reviewed_by: str, note: str | None,
                        sig: dict, materialised: str | None = None, now: str | None = None) -> None:
        self._q("UPDATE proposals SET state=?, reviewed_by=?, reviewed_at=?, review_note=?, "
                "sig_alg=?, sig_key_id=?, sig_payload=?, sig_value=?, materialised_record_id=? WHERE id=?",
                (state, reviewed_by, now or rfc3339(utcnow()), note, sig.get("alg", "ed25519"),
                 sig["key_id"], sig["signed_payload"], sig["sig"], materialised, pid))

    def expire_proposals(self, before: str) -> int:
        rows = self._q("SELECT id FROM proposals WHERE state='pending' AND proposed_at < ?", (before,))
        for r in rows:
            self._q("UPDATE proposals SET state='expired' WHERE id=?", (r["id"],))
        return len(rows)

    # -- challenges --------------------------------------------------------
    def issue_challenge(self, *, nonce: str, proposal_id: str, scope: str, candidate_sha256: str,
                        expires_at: str, now: str | None = None) -> None:
        self._q("INSERT INTO approval_challenges (nonce,proposal_id,scope,candidate_sha256,"
                "issued_at,expires_at) VALUES (?,?,?,?,?,?)",
                (nonce, proposal_id, scope, candidate_sha256, now or rfc3339(utcnow()), expires_at))

    def consume_challenge(self, nonce: str, proposal_id: str, by: str,
                          now: datetime | None = None) -> dict | None:
        """Single-use. Marked consumed rather than deleted, so a replay attempt is
        visible in the record instead of just failing."""
        rows = self._q("SELECT * FROM approval_challenges WHERE nonce=? AND proposal_id=?",
                       (nonce, proposal_id))
        if not rows:
            return None
        ch = dict(rows[0])
        if ch["consumed_at"] is not None or ch["expires_at"] <= rfc3339(now or utcnow()):
            return ch  # caller inspects and raises the right error
        self._q("UPDATE approval_challenges SET consumed_at=?, consumed_by=? WHERE nonce=?",
                (rfc3339(now or utcnow()), by, nonce))
        return ch

    # -- forgetting --------------------------------------------------------
    def set_status(self, ids: list[str], status: str) -> int:
        if not ids:
            return 0
        self._q(f"UPDATE records SET status=? WHERE id IN ({','.join('?' * len(ids))})", (status, *ids))
        return len(ids)

    def redact(self, ids: list[str], now: str | None = None) -> int:
        for rid in ids:
            self._q("UPDATE records SET content='[redacted]', payload=NULL, status='redacted', "
                    "updated_at=? WHERE id=?", (now or rfc3339(utcnow()), rid))
            self._q("UPDATE episodic_attrs SET action_input=NULL, observation='[redacted]' "
                    "WHERE record_id=?", (rid,))
        return len(ids)

    def hard_delete(self, ids: list[str]) -> int:
        if not ids:
            return 0
        seqs = [r["seq"] for r in self._q(
            f"SELECT seq FROM records WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))]
        self._q("BEGIN")
        try:
            if self.vector_ok and seqs:
                self._q(f"DELETE FROM records_vec WHERE rowid IN ({','.join('?' * len(seqs))})", tuple(seqs))
            self._q(f"DELETE FROM records WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
            self._q("COMMIT")
        except Exception:
            self._q("ROLLBACK")
            raise
        return len(ids)

    def select_for_forget(self, scope: str, filters: dict) -> list[str]:
        sql = "SELECT r.id FROM records r WHERE r.scope=? AND r.status != 'tombstoned'"
        args: list = [scope]
        if filters.get("types"):
            sql += " AND r.type IN (%s)" % ",".join("?" * len(filters["types"]))
            args += filters["types"]
        if filters.get("older_than"):
            sql += " AND r.created_at < ?"
            args.append(filters["older_than"])
        if filters.get("never_accessed"):
            sql += " AND r.access_count = 0"
        for tag in filters.get("tags") or []:
            sql += " AND EXISTS (SELECT 1 FROM json_each(r.tags) WHERE value=?)"
            args.append(tag)
        if filters.get("session_id"):
            sql += (" AND EXISTS (SELECT 1 FROM episodic_attrs e WHERE e.record_id=r.id "
                    "AND e.session_id=?)")
            args.append(filters["session_id"])
        if filters.get("sensitivity"):
            sql += (" AND EXISTS (SELECT 1 FROM semantic_attrs s WHERE s.record_id=r.id "
                    "AND s.sensitivity=?)")
            args.append(filters["sensitivity"])
        return [r["id"] for r in self._q(sql, tuple(args))]

    # -- reflection inputs -------------------------------------------------
    def episodes_for_reflection(self, scope: str, since: str | None, session_id: str | None,
                                limit: int) -> list[dict]:
        sql = ("SELECT r.id, r.content, r.created_at, r.importance, e.outcome, e.reward, "
               "e.session_id, e.cycle_id, e.action_name FROM records r "
               "JOIN episodic_attrs e ON e.record_id=r.id "
               "WHERE r.scope=? AND r.status='active'")
        args: list = [scope]
        if since:
            sql += " AND r.created_at >= ?"
            args.append(since)
        if session_id:
            sql += " AND e.session_id=?"
            args.append(session_id)
        return [dict(r) for r in self._q(sql + " ORDER BY r.created_at LIMIT ?", (*args, limit + 1))]

    def triple_conflicts(self, scope: str) -> list[dict]:
        """Same subject+predicate, different object, both live. Mechanical
        detection - the reason filling the triple form is worth doing."""
        return [dict(r) for r in self._q(
            "SELECT a.record_id ra, b.record_id rb, a.subject, a.predicate, a.object oa, b.object ob "
            "FROM semantic_attrs a JOIN semantic_attrs b "
            "  ON a.subject=b.subject AND a.predicate=b.predicate AND a.object < b.object "
            "JOIN records ra_ ON ra_.id=a.record_id AND ra_.status='active' AND ra_.scope=? "
            "JOIN records rb_ ON rb_.id=b.record_id AND rb_.status='active' AND rb_.scope=? "
            "WHERE a.subject IS NOT NULL AND (a.valid_to IS NULL AND b.valid_to IS NULL)",
            (scope, scope))]

    def find_semantic_by_triple(self, scope: str, subject: str, predicate: str) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT r.id, r.content, s.object FROM records r JOIN semantic_attrs s ON s.record_id=r.id "
            "WHERE r.scope=? AND r.status='active' AND s.subject=? AND s.predicate=?",
            (scope, subject, predicate))]

    def approved_procedures(self, scope: str | None = None) -> list[dict]:
        sql = "SELECT * FROM v_approval_audit"
        args: tuple = ()
        if scope:
            sql += " WHERE scope=?"
            args = (scope,)
        return [dict(r) for r in self._q(sql, args)]

    def procedure_candidate(self, record_id: str) -> dict:
        row = self._q("SELECT * FROM procedural_attrs WHERE record_id=?", (record_id,))[0]
        return {
            "trigger": row["trigger_text"],
            "preconditions": json.loads(row["preconditions"] or "[]"),
            "steps": json.loads(row["steps"]),
            "success_signal": row["success_signal"],
            "failure_signal": row["failure_signal"],
        }

    def expired_records(self, now: str | None = None) -> list[str]:
        return [r["id"] for r in self._q(
            "SELECT id FROM records WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now or rfc3339(utcnow()),))]

    def unembedded(self, model: str, limit: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT id, seq, content FROM records WHERE status='active' "
            "AND (embedding_model IS NULL OR embedding_model != ?) LIMIT ?", (model, limit))]

    def set_embedding(self, seq: int, rid: str, embedding: list[float], model: str) -> None:
        if not self.vector_ok:
            return
        self._q("DELETE FROM records_vec WHERE rowid=?", (seq,))
        self._q("INSERT INTO records_vec (rowid, embedding) VALUES (?,?)", (seq, serialize(embedding)))
        self._q("UPDATE records SET embedding_model=? WHERE id=?", (model, rid))

    # -- stats -------------------------------------------------------------
    def stats(self, scope: str | None) -> dict:
        where, args = ("WHERE scope=?", (scope,)) if scope else ("", ())
        by_type = {r["type"]: r["c"] for r in self._q(
            f"SELECT type, count(*) c FROM records {where} GROUP BY type", args)}
        by_status = {r["status"]: r["c"] for r in self._q(
            f"SELECT status, count(*) c FROM records {where} GROUP BY status", args)}
        total = sum(by_type.values())
        embedded = self._q(
            f"SELECT count(*) c FROM records {where or 'WHERE 1=1'} "
            f"{'AND' if where else 'AND'} status='active' AND embedding_model IS NOT NULL", args)[0]["c"]
        active = by_status.get("active", 0)
        return {
            "by_type": {k: by_type.get(k, 0) for k in ("episodic", "semantic", "procedural")},
            "by_status": {k: by_status.get(k, 0) for k in
                          ("active", "superseded", "tombstoned", "redacted")},
            "total": total,
            "embedded": embedded,
            "active": active,
            "db_size_bytes": Path(self.path).stat().st_size if self.path != ":memory:" else 0,
            "pending_proposals": self.queue_depth(scope),
            "oldest_pending_at": (self._q(
                "SELECT min(proposed_at) m FROM proposals WHERE state='pending'")[0]["m"]),
            "open_cycles": self._q("SELECT count(*) c FROM working_set WHERE status='open'")[0]["c"],
            "stale_cycles": self._q("SELECT count(*) c FROM v_stale_cycles")[0]["c"],
            "failing_procedures": self._q("SELECT count(*) c FROM v_failing_procedures")[0]["c"],
            "sensitive": self._q(
                "SELECT count(*) c FROM semantic_attrs WHERE sensitivity != 'none'")[0]["c"],
        }

    def top_accessed(self, scope: str | None, limit: int) -> list[dict]:
        where, args = ("WHERE scope=?", (scope,)) if scope else ("", ())
        return [dict(r) for r in self._q(
            f"SELECT id, content, access_count FROM records {where} "
            f"ORDER BY access_count DESC LIMIT ?", (*args, limit))]

    def scopes(self) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT scope, count(*) total FROM records GROUP BY scope ORDER BY total DESC")]


def _fts_query(text: str) -> str:
    """Quote each term so user text cannot become FTS5 operator syntax."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'
