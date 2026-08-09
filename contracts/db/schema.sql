-- =============================================================================
-- Memory Agent (CoALA) — SQLite storage contract, v1.0
-- =============================================================================
--
-- One file on disk is the entire long-term memory of the agent. Copy the file,
-- copy the memory. That portability is the reason SQLite was chosen over a
-- hosted store, and the reason this schema avoids anything exotic.
--
-- Layout mirrors the CoALA memory modules:
--
--   records          base envelope, one row per long-term memory record
--   + episodic_attrs   what happened      (CoALA episodic)
--   + semantic_attrs   what is true       (CoALA semantic)
--   + procedural_attrs how to do it       (CoALA procedural, approval-gated)
--   working_set      one open decision cycle (CoALA working memory, TTL'd)
--   observations     append-only log within a cycle
--   proposals        candidate writes awaiting a decision — a SEPARATE TABLE,
--                    not a status flag, so nothing unapproved can leak into
--                    recall through a forgotten WHERE clause
--   records_fts      FTS5 keyword index over records.content
--   records_vec      sqlite-vec vector index over records.content embeddings
--   meta             singleton config/versioning
--
-- Extensions:
--   FTS5       built into standard SQLite builds. Required.
--   sqlite-vec provides vec0. Required for semantic recall; if it fails to
--              load, recall degrades to keyword-only and reports
--              degraded.reason = "vector_unavailable" rather than returning
--              an empty result. Everything below the VECTOR INDEX marker is
--              the only part that needs the extension.
--
-- Apply with:  sqlite3 memory.db < schema.sql
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;      -- many readers, one writer; see NF-3 in the spec
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;


-- =============================================================================
-- meta
-- =============================================================================

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta (key, value) VALUES
  ('schema_version',  '1.0'),
  ('embedding_model', ''),          -- set on first write; a change triggers re-embed
  ('embedding_dim',   '384'),       -- MUST match the vec0 declaration below
  ('created_at',      strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));


-- =============================================================================
-- records — the memory envelope
-- =============================================================================

CREATE TABLE IF NOT EXISTS records (
  -- seq is the join key for the FTS and vector indexes. id is the public
  -- identifier that appears in the API; nothing outside this file uses seq.
  seq              INTEGER PRIMARY KEY,
  id               TEXT    NOT NULL UNIQUE,
  schema_version   TEXT    NOT NULL DEFAULT '1.0',

  type             TEXT    NOT NULL CHECK (type IN ('episodic','semantic','procedural')),
  scope            TEXT    NOT NULL CHECK (scope GLOB '[a-z0-9]*'),

  -- content is the canonical searchable surface form. Writer-side invariant:
  -- anything a reader needs must be here, because this is the ONLY column that
  -- is embedded and keyword-indexed. Type-specific fields duplicate into it.
  content          TEXT    NOT NULL CHECK (length(content) BETWEEN 1 AND 8192),
  payload          TEXT             CHECK (payload IS NULL OR json_valid(payload)),

  created_at       TEXT    NOT NULL,
  updated_at       TEXT,
  last_accessed_at TEXT,
  access_count     INTEGER NOT NULL DEFAULT 0 CHECK (access_count >= 0),

  importance       REAL    NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  confidence       REAL    NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),

  provenance       TEXT             CHECK (provenance IS NULL OR json_valid(provenance)),
  tags             TEXT             CHECK (tags IS NULL OR json_valid(tags)),

  expires_at       TEXT,
  status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','superseded','tombstoned','redacted')),
  superseded_by    TEXT    REFERENCES records(id) ON DELETE SET NULL,

  -- The loop-safety primitive. Unique per scope (partial index below), so a
  -- retried iteration of a host loop returns the original row instead of
  -- writing a near-duplicate.
  idempotency_key  TEXT,

  embedding_model  TEXT,

  -- A superseded record must name its successor, and only a superseded record
  -- may have one. Prevents the half-applied supersession that makes a memory
  -- store quietly return stale facts.
  CHECK ((status = 'superseded') = (superseded_by IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_records_idem
  ON records (scope, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_records_lookup   ON records (scope, type, status);
CREATE INDEX IF NOT EXISTS ix_records_expiry   ON records (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_records_decay    ON records (status, last_accessed_at);
CREATE INDEX IF NOT EXISTS ix_records_embedmod ON records (embedding_model);


-- =============================================================================
-- episodic_attrs — CoALA episodic memory
-- =============================================================================
-- The (session_id, cycle_id, step_no) triple is unique and monotonic. That is
-- what lets a trajectory be replayed exactly after a crash or a retry.

CREATE TABLE IF NOT EXISTS episodic_attrs (
  record_id       TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,

  session_id      TEXT    NOT NULL,
  cycle_id        TEXT    NOT NULL,
  parent_cycle_id TEXT,
  step_no         INTEGER NOT NULL CHECK (step_no >= 0),

  goal            TEXT,
  action_class    TEXT    CHECK (action_class IS NULL OR action_class IN (
                      'reasoning','retrieval','learning',
                      'grounding.digital','grounding.dialogue','grounding.physical')),
  action_name     TEXT,
  action_input    TEXT    CHECK (action_input IS NULL OR json_valid(action_input)),
  observation     TEXT,

  outcome         TEXT    NOT NULL DEFAULT 'unknown'
                    CHECK (outcome IN ('success','failure','partial','abandoned','unknown')),
  reward          REAL    CHECK (reward IS NULL OR reward BETWEEN -1 AND 1),
  error           TEXT    CHECK (error IS NULL OR json_valid(error)),
  duration_ms     INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
  tokens          TEXT    CHECK (tokens IS NULL OR json_valid(tokens))
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_episodic_step
  ON episodic_attrs (session_id, cycle_id, step_no);
CREATE INDEX IF NOT EXISTS ix_episodic_cycle  ON episodic_attrs (cycle_id, step_no);
CREATE INDEX IF NOT EXISTS ix_episodic_parent ON episodic_attrs (parent_cycle_id)
  WHERE parent_cycle_id IS NOT NULL;
-- Repeated identical failures are the trigger for a procedural proposal.
CREATE INDEX IF NOT EXISTS ix_episodic_failure ON episodic_attrs (outcome, action_name)
  WHERE outcome = 'failure';


-- =============================================================================
-- semantic_attrs — CoALA semantic memory
-- =============================================================================

CREATE TABLE IF NOT EXISTS semantic_attrs (
  record_id      TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,

  -- Optional triple form. When present, contradiction detection is mechanical:
  -- same subject+predicate, different object. When absent it must be semantic,
  -- which is slower and less reliable — so writers are encouraged to fill it.
  subject        TEXT,
  predicate      TEXT,
  object         TEXT,

  valid_from     TEXT,
  valid_to       TEXT,

  contradicts    TEXT    CHECK (contradicts IS NULL OR json_valid(contradicts)),
  corroborations INTEGER NOT NULL DEFAULT 1 CHECK (corroborations >= 0),

  volatility     TEXT    NOT NULL DEFAULT 'slow'
                   CHECK (volatility IN ('stable','slow','volatile')),
  sensitivity    TEXT    NOT NULL DEFAULT 'none'
                   CHECK (sensitivity IN ('none','pii','secret')),

  CHECK ((subject IS NULL) = (predicate IS NULL))
);

CREATE INDEX IF NOT EXISTS ix_semantic_triple ON semantic_attrs (subject, predicate)
  WHERE subject IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_semantic_valid  ON semantic_attrs (valid_to);
-- Drives the redaction sweep.
CREATE INDEX IF NOT EXISTS ix_semantic_sens   ON semantic_attrs (sensitivity)
  WHERE sensitivity != 'none';


-- =============================================================================
-- procedural_attrs — CoALA procedural memory
-- =============================================================================
-- Narrowed on purpose. CoALA's procedural memory includes the agent's own code
-- and the authors flag rewriting it as the riskiest learning modality. Here a
-- procedure is DATA a host may choose to follow: natural-language steps, never
-- executable code, never applied to the memory agent itself.
--
-- A row existing in this table means it was already approved. Unapproved
-- candidates live in `proposals` and cannot be reached by recall at all.

CREATE TABLE IF NOT EXISTS procedural_attrs (
  record_id      TEXT PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,

  trigger_text   TEXT    NOT NULL,
  preconditions  TEXT    CHECK (preconditions IS NULL OR json_valid(preconditions)),
  steps          TEXT    NOT NULL CHECK (json_valid(steps) AND json_array_length(steps) >= 1),
  success_signal TEXT,
  failure_signal TEXT,

  approval_state TEXT    NOT NULL CHECK (approval_state IN ('approved','rejected')),
  reviewed_by    TEXT    NOT NULL,
  reviewed_at    TEXT    NOT NULL,
  rationale      TEXT,

  -- Proof the decision came from a specific human, not from an agent typing a
  -- name. reviewed_by above is a claim; these columns are the evidence.
  --
  -- All NOT NULL, so an unsigned approved procedure is unrepresentable in this
  -- table rather than merely discouraged - the same trick as keeping unapproved
  -- candidates out of `records` entirely.
  --
  -- sig_payload holds the signed bytes VERBATIM. Verification must never
  -- rebuild the payload from the columns around it: that would only prove the
  -- row is self-consistent. Storing it as signed is what lets anyone re-verify
  -- this decision years later with nothing but the public key.
  sig_alg        TEXT    NOT NULL DEFAULT 'ed25519' CHECK (sig_alg = 'ed25519'),
  sig_key_id     TEXT    NOT NULL CHECK (sig_key_id GLOB 'SHA256:*'),
  sig_payload    TEXT    NOT NULL,
  sig_value      TEXT    NOT NULL,
  -- Candidate hash at signing time. If this stops matching the live record, the
  -- procedure was edited after approval and must be treated as unapproved.
  candidate_sha256 TEXT  NOT NULL CHECK (length(candidate_sha256) = 64),
  sig_verified_at  TEXT,

  -- Does the learned procedure actually work? A high invocation count with a
  -- low success count means it should be revised or retired.
  invocations    INTEGER NOT NULL DEFAULT 0 CHECK (invocations >= 0),
  successes      INTEGER NOT NULL DEFAULT 0 CHECK (successes >= 0),
  last_used_at   TEXT,

  supersedes     TEXT REFERENCES records(id) ON DELETE SET NULL,

  CHECK (successes <= invocations)
);

CREATE INDEX IF NOT EXISTS ix_procedural_state ON procedural_attrs (approval_state);
CREATE INDEX IF NOT EXISTS ix_procedural_usage ON procedural_attrs (invocations, successes);
-- Find everything approved by a key that was later retired or compromised.
CREATE INDEX IF NOT EXISTS ix_procedural_key   ON procedural_attrs (sig_key_id);


-- =============================================================================
-- approval_challenges — server-issued nonces
-- =============================================================================
-- A nonce binds one signature to one decision on one proposal within a short
-- window. Without it a reviewer could be induced to pre-sign approvals, or an
-- old signature could be held and replayed after the candidate changed.
--
-- Issued by memory_review_proposals(action='list'), consumed exactly once by
-- the matching approve/reject. Single-use is enforced by `consumed_at` rather
-- than by deletion, so a replay attempt is visible instead of just failing.

CREATE TABLE IF NOT EXISTS approval_challenges (
  nonce       TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
  scope       TEXT NOT NULL,
  -- Candidate hash at the moment the challenge was issued. If the candidate
  -- changes before the signature arrives, the hashes diverge and the signature
  -- is refused - a reviewer only ever approves what they were actually shown.
  candidate_sha256 TEXT NOT NULL CHECK (length(candidate_sha256) = 64),
  issued_at   TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  consumed_at TEXT,
  consumed_by TEXT
);

CREATE INDEX IF NOT EXISTS ix_challenges_open ON approval_challenges (proposal_id, expires_at)
  WHERE consumed_at IS NULL;


-- =============================================================================
-- working_set + observations — CoALA working memory
-- =============================================================================
-- Deliberately not durable knowledge. This is a scratch buffer that exists so a
-- loop can survive a crash, so a trajectory can be reconstructed, and so a
-- runaway loop can be spotted. It expires. Anything worth keeping is promoted
-- into `records` on close.

CREATE TABLE IF NOT EXISTS working_set (
  cycle_id        TEXT PRIMARY KEY,
  schema_version  TEXT NOT NULL DEFAULT '1.0',

  session_id      TEXT NOT NULL,
  parent_cycle_id TEXT REFERENCES working_set(cycle_id) ON DELETE SET NULL,
  scope           TEXT NOT NULL,

  goal            TEXT NOT NULL,

  -- Stable hash of the normalised goal plus the first action. Two cycles with
  -- the same signature and no change in outcome is the definition of a stuck
  -- loop; this column makes that check an index lookup rather than a scan.
  loop_signature  TEXT,

  status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','closed','abandoned')),
  outcome         TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (outcome IN ('success','failure','partial','abandoned','unknown')),

  opened_at       TEXT NOT NULL,
  closed_at       TEXT,
  expires_at      TEXT NOT NULL,

  -- Candidate vs. actually-injected records, so recall quality stays auditable.
  retrieved       TEXT CHECK (retrieved IS NULL OR json_valid(retrieved)),

  CHECK ((status = 'open') = (closed_at IS NULL))
);

CREATE INDEX IF NOT EXISTS ix_ws_session ON working_set (session_id, opened_at);
CREATE INDEX IF NOT EXISTS ix_ws_reap    ON working_set (status, expires_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_ws_loop    ON working_set (scope, loop_signature, opened_at)
  WHERE loop_signature IS NOT NULL;

-- Append-only. Written BEFORE the host acts on a result, so a crash mid-cycle
-- leaves evidence instead of a hole. A separate table rather than a JSON column
-- because this is the hottest write path in an agentic loop.
CREATE TABLE IF NOT EXISTS observations (
  cycle_id TEXT    NOT NULL REFERENCES working_set(cycle_id) ON DELETE CASCADE,
  step_no  INTEGER NOT NULL CHECK (step_no >= 0),
  at       TEXT    NOT NULL,
  text     TEXT    NOT NULL,
  PRIMARY KEY (cycle_id, step_no)
);


-- =============================================================================
-- proposals — the approval gate
-- =============================================================================
-- Candidate writes that are not yet part of memory. Kept in their own table
-- rather than as a status flag on `records`, so an unapproved candidate cannot
-- reach recall even if a query forgets its filter. Approving materialises the
-- candidate into `records` (+ its attrs table) inside one transaction.
--
-- Every procedural write lands here first, always, including ones generated by
-- the unattended daemon. The daemon may create proposals; it may never approve
-- one.

CREATE TABLE IF NOT EXISTS proposals (
  id           TEXT PRIMARY KEY,
  scope        TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('procedural','semantic','forget','supersede')),

  -- The full candidate record, validated against its JSON Schema on insert.
  candidate    TEXT NOT NULL CHECK (json_valid(candidate)),
  rationale    TEXT,

  proposed_by  TEXT NOT NULL,
  proposed_at  TEXT NOT NULL,
  source       TEXT NOT NULL CHECK (source IN ('host','daemon','human','import')),

  state        TEXT NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending','approved','rejected','expired')),
  reviewed_by  TEXT,
  reviewed_at  TEXT,
  review_note  TEXT,

  -- Signature over the decision. Approvals also copy this onto the resulting
  -- procedural_attrs row; rejections live only here, and are signed too - a
  -- forged rejection is a denial of service on the agent's own learning, and it
  -- puts a human's name against a decision they did not make.
  sig_alg      TEXT CHECK (sig_alg IS NULL OR sig_alg = 'ed25519'),
  sig_key_id   TEXT,
  sig_payload  TEXT,
  sig_value    TEXT,

  -- Set when approval materialised the candidate, so the resulting record can
  -- be traced back to the decision that let it in.
  materialised_record_id TEXT REFERENCES records(id) ON DELETE SET NULL,

  -- Stops a daemon from re-proposing the same candidate every run.
  dedupe_key   TEXT,

  CHECK ((state = 'pending') = (reviewed_by IS NULL)),
  CHECK ((state = 'approved') OR (materialised_record_id IS NULL)),
  -- A human decision must carry its proof. 'expired' is the server's own
  -- bookkeeping, not a human decision, so it is exempt.
  CHECK (state IN ('pending','expired')
         OR (sig_key_id IS NOT NULL AND sig_payload IS NOT NULL AND sig_value IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_proposals_queue ON proposals (state, scope, proposed_at);
CREATE UNIQUE INDEX IF NOT EXISTS ix_proposals_dedupe
  ON proposals (scope, dedupe_key)
  WHERE dedupe_key IS NOT NULL AND state = 'pending';


-- =============================================================================
-- KEYWORD INDEX — FTS5 (built in, always available)
-- =============================================================================
-- External-content table over records.content. Contributes the BM25 half of
-- hybrid retrieval.

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  content,
  content = 'records',
  content_rowid = 'seq',
  tokenize = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_records_fts_ai AFTER INSERT ON records BEGIN
  INSERT INTO records_fts(rowid, content) VALUES (new.seq, new.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_records_fts_ad AFTER DELETE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, content) VALUES ('delete', old.seq, old.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_records_fts_au AFTER UPDATE OF content ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, content) VALUES ('delete', old.seq, old.content);
  INSERT INTO records_fts(rowid, content) VALUES (new.seq, new.content);
END;

-- Keep updated_at honest without making every writer remember to set it.
CREATE TRIGGER IF NOT EXISTS trg_records_touch
AFTER UPDATE OF content, status, importance, confidence, tags, payload ON records
BEGIN
  UPDATE records SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE seq = new.seq;
END;


-- =============================================================================
-- VECTOR INDEX — sqlite-vec (requires the extension to be loaded)
-- =============================================================================
-- Everything above this marker runs on a stock SQLite build. Only the statement
-- below needs sqlite-vec. If the extension is unavailable, skip it: recall
-- falls back to keyword-only and reports degraded.reason="vector_unavailable".
--
-- rowid here is records.seq. The dimension MUST equal meta.embedding_dim;
-- changing the embedding model means recreating this table and re-embedding,
-- which is what the embedding_model column on records exists to detect.
--
--   .load ./vec0
--   CREATE VIRTUAL TABLE IF NOT EXISTS records_vec USING vec0(
--     embedding float[384]
--   );
--
-- Deletion is not cascaded by trigger, because a trigger cannot be created
-- against a virtual table that may not exist. The storage layer deletes from
-- records_vec in the same transaction as records. Orphan sweep for safety:
--
--   DELETE FROM records_vec
--   WHERE rowid NOT IN (SELECT seq FROM records);


-- =============================================================================
-- VIEWS
-- =============================================================================

-- The only rows recall is ever allowed to consider. Expired-but-not-yet-swept
-- records are excluded here rather than relying on the daemon having run.
CREATE VIEW IF NOT EXISTS v_recallable AS
SELECT r.*
FROM records r
WHERE r.status = 'active'
  AND (r.expires_at IS NULL OR r.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- Semantic facts whose validity window has closed. Retained, not deleted:
-- knowing a fact used to be true is itself useful.
CREATE VIEW IF NOT EXISTS v_expired_facts AS
SELECT r.id, r.content, s.valid_to
FROM records r JOIN semantic_attrs s ON s.record_id = r.id
WHERE s.valid_to IS NOT NULL
  AND s.valid_to <= strftime('%Y-%m-%dT%H:%M:%SZ','now');

-- Procedures that keep being used and keep failing.
CREATE VIEW IF NOT EXISTS v_failing_procedures AS
SELECT r.id, r.content, p.invocations, p.successes,
       CAST(p.successes AS REAL) / p.invocations AS success_rate
FROM records r JOIN procedural_attrs p ON p.record_id = r.id
WHERE p.invocations >= 5
  AND CAST(p.successes AS REAL) / p.invocations < 0.5;

-- Procedures whose stored content no longer hashes to what was signed, i.e.
-- edited after approval. The application computes the live hash; this view
-- surfaces the columns to compare and the key that signed. Anything appearing
-- here must be treated as UNAPPROVED until re-signed.
CREATE VIEW IF NOT EXISTS v_approval_audit AS
SELECT r.id, r.scope, r.content, p.reviewed_by, p.reviewed_at,
       p.sig_key_id, p.candidate_sha256, p.sig_payload, p.sig_value, p.sig_verified_at
FROM records r JOIN procedural_attrs p ON p.record_id = r.id
WHERE p.approval_state = 'approved';

-- The human review queue.
CREATE VIEW IF NOT EXISTS v_pending_proposals AS
SELECT id, scope, kind, rationale, proposed_by, proposed_at, source, candidate
FROM proposals
WHERE state = 'pending'
ORDER BY proposed_at;

-- Cycles opened and never closed. The daemon reaps these to 'abandoned' rather
-- than discarding them — an unclosed cycle is evidence of a crash.
CREATE VIEW IF NOT EXISTS v_stale_cycles AS
SELECT cycle_id, session_id, scope, goal, opened_at, expires_at
FROM working_set
WHERE status = 'open'
  AND expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now');
