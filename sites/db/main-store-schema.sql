-- Additive, owner-only authoritative store. Existing Site display pointers are untouched.
-- All historical revisions, records, object chunks, files and manifests are retained.
CREATE TABLE IF NOT EXISTS ms_heads (
 namespace TEXT PRIMARY KEY CHECK(namespace='owner'), revision INTEGER NOT NULL DEFAULT 0,
 import_id TEXT, operation_id TEXT, updated_at TEXT
);
INSERT OR IGNORE INTO ms_heads(namespace,revision) VALUES('owner',0);
CREATE TABLE IF NOT EXISTS ms_chunks (
 namespace TEXT NOT NULL CHECK(namespace='owner'), sha256 TEXT NOT NULL,
 bytes INTEGER NOT NULL, data_base64 TEXT NOT NULL, PRIMARY KEY(namespace,sha256)
);
CREATE TABLE IF NOT EXISTS ms_objects (
 namespace TEXT NOT NULL CHECK(namespace='owner'), sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
 descriptor TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0, hash_state TEXT NOT NULL,
 verified INTEGER NOT NULL DEFAULT 0, valid_utf8 INTEGER NOT NULL DEFAULT 0,
 utf8_state TEXT NOT NULL DEFAULT '{"needed":0,"min":128,"max":191,"valid":true}', PRIMARY KEY(namespace,sha256)
);
CREATE TABLE IF NOT EXISTS ms_imports (
 namespace TEXT NOT NULL CHECK(namespace='owner'), import_id TEXT NOT NULL,
 manifest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'staging', created_at TEXT NOT NULL,
 PRIMARY KEY(namespace,import_id)
);
CREATE TABLE IF NOT EXISTS ms_import_tables (
 namespace TEXT NOT NULL, import_id TEXT NOT NULL, table_name TEXT NOT NULL,
 row_count INTEGER NOT NULL, expected_chain TEXT NOT NULL, metadata TEXT NOT NULL,
 cursor TEXT NOT NULL DEFAULT '', checked_count INTEGER NOT NULL DEFAULT 0,
 chain TEXT NOT NULL, hash_state TEXT, checked_bytes INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,import_id,table_name)
);
CREATE TABLE IF NOT EXISTS ms_import_batches (
 namespace TEXT NOT NULL, import_id TEXT NOT NULL, operation_id TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, PRIMARY KEY(namespace,import_id,operation_id)
);
CREATE TABLE IF NOT EXISTS ms_records (
 namespace TEXT NOT NULL CHECK(namespace='owner'), record_sha256 TEXT NOT NULL,
 envelope TEXT NOT NULL, PRIMARY KEY(namespace,record_sha256)
);
CREATE TABLE IF NOT EXISTS ms_rows (
 namespace TEXT NOT NULL, import_id TEXT NOT NULL, table_name TEXT NOT NULL,
 key_sha256 TEXT NOT NULL, revision INTEGER NOT NULL, record_sha256 TEXT,
 identity TEXT, run_id TEXT, status TEXT, task_type TEXT, profile_hash TEXT, provider TEXT,
 updated_at TEXT, sequence_value INTEGER, projection TEXT NOT NULL,
 PRIMARY KEY(namespace,import_id,table_name,key_sha256,revision)
);
CREATE INDEX IF NOT EXISTS ms_rows_lookup ON ms_rows(namespace,import_id,table_name,identity,revision);
CREATE INDEX IF NOT EXISTS ms_rows_runs ON ms_rows(namespace,import_id,table_name,run_id,revision);
CREATE INDEX IF NOT EXISTS ms_rows_status ON ms_rows(namespace,import_id,table_name,status,revision);
CREATE TABLE IF NOT EXISTS ms_files (
 namespace TEXT NOT NULL, import_id TEXT NOT NULL, path TEXT NOT NULL, revision INTEGER NOT NULL,
 sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, mode INTEGER NOT NULL,
 PRIMARY KEY(namespace,import_id,path,revision)
);
CREATE TABLE IF NOT EXISTS ms_revisions (
 namespace TEXT NOT NULL CHECK(namespace='owner'), revision INTEGER NOT NULL,
 import_id TEXT NOT NULL, operation_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
 watermarks TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(namespace,revision),
 UNIQUE(namespace,operation_id)
);
CREATE TABLE IF NOT EXISTS ms_operations (
 namespace TEXT NOT NULL CHECK(namespace='owner'), operation_id TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, revision INTEGER NOT NULL, noop INTEGER NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(namespace,operation_id)
);
CREATE TABLE IF NOT EXISTS ms_operation_stages (
 namespace TEXT NOT NULL, operation_id TEXT NOT NULL, expected_revision INTEGER NOT NULL,
 import_id TEXT NOT NULL, mutation_count INTEGER NOT NULL, file_count INTEGER NOT NULL,
 expected_chain TEXT NOT NULL, watermarks TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'staging', cursor TEXT NOT NULL DEFAULT '',
 chain TEXT NOT NULL, checked_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(namespace,operation_id)
);
CREATE TABLE IF NOT EXISTS ms_staged_items (
 namespace TEXT NOT NULL, operation_id TEXT NOT NULL, item_key TEXT NOT NULL,
 kind TEXT NOT NULL, item_sha256 TEXT NOT NULL, payload TEXT NOT NULL,
 normalized TEXT NOT NULL, PRIMARY KEY(namespace,operation_id,item_key)
);
CREATE TABLE IF NOT EXISTS ms_stage_batches (
 namespace TEXT NOT NULL, operation_id TEXT NOT NULL, batch_id TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, PRIMARY KEY(namespace,operation_id,batch_id)
);
