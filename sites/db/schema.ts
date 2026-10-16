import { sql } from "drizzle-orm";
import { integer, sqliteTable, text, primaryKey, index, unique, check } from "drizzle-orm/sqlite-core";
export const generations = sqliteTable("generations", {
  id: text("id").primaryKey(), count: integer("count").notNull(), createdAt: text("created_at").notNull(),
  status: text("status").notNull().default("staging"),
});
export const records = sqliteTable("records", {
  generation: text("generation").notNull(), kind: text("kind").notNull(), key: text("key").notNull(),
  data: text("data").notNull(), checksum: text("checksum").notNull(),
}, (table) => [primaryKey({columns: [table.generation, table.kind, table.key]})]);
export const pointers = sqliteTable("pointers", { key: text("key").primaryKey(), value: text("value").notNull() });
export const displayPayloads = sqliteTable("display_payloads", {checksum: text("checksum").primaryKey(), data: text("data").notNull()});
export const displayGenerations = sqliteTable("display_generations", {generation: text("generation").primaryKey()});
export const cloudFeedback = sqliteTable("cloud_feedback", {
  seq: integer("seq").primaryKey({autoIncrement: true}), identity: text("identity").notNull(),
  interest: text("interest"), reason: text("reason"), favorite: integer("favorite").notNull(),
  readingStatus: text("reading_status").notNull(), updatedAt: text("updated_at").notNull(),
});
export const cloudProfileEvents = sqliteTable("cloud_profile_events", {
  seq: integer("seq").primaryKey(), baseVersion: integer("base_version").notNull(),
  baseGeneration: text("base_generation").notNull(), baseProfileHash: text("base_profile_hash").notNull(),
  basePreferences: text("base_preferences").notNull(), profileHash: text("profile_hash").notNull(),
  content: text("content").notNull(), screeningPreferences: text("screening_preferences").notNull(),
  summary: text("summary").notNull(), updatedAt: text("updated_at").notNull(),
});

export const ms_chunks = sqliteTable("ms_chunks", {
  namespace: text("namespace").notNull(),
  sha256: text("sha256").notNull(),
  bytes: integer("bytes").notNull(),
  data_base64: text("data_base64").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.sha256]}),
  check("ms_chunks_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_files = sqliteTable("ms_files", {
  namespace: text("namespace").notNull(),
  import_id: text("import_id").notNull(),
  path: text("path").notNull(),
  revision: integer("revision").notNull(),
  sha256: text("sha256").notNull(),
  bytes: integer("bytes").notNull(),
  mode: integer("mode").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.import_id, table.path, table.revision]})
]);

export const ms_heads = sqliteTable("ms_heads", {
  namespace: text("namespace"),
  revision: integer("revision").notNull().default(0),
  import_id: text("import_id"),
  operation_id: text("operation_id"),
  updated_at: text("updated_at"),
}, (table) => [
  primaryKey({columns: [table.namespace]}),
  check("ms_heads_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_import_batches = sqliteTable("ms_import_batches", {
  namespace: text("namespace").notNull(),
  import_id: text("import_id").notNull(),
  operation_id: text("operation_id").notNull(),
  payload_sha256: text("payload_sha256").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.import_id, table.operation_id]})
]);

export const ms_import_tables = sqliteTable("ms_import_tables", {
  namespace: text("namespace").notNull(),
  import_id: text("import_id").notNull(),
  table_name: text("table_name").notNull(),
  row_count: integer("row_count").notNull(),
  expected_chain: text("expected_chain").notNull(),
  metadata: text("metadata").notNull(),
  cursor: text("cursor").notNull().default(""),
  checked_count: integer("checked_count").notNull().default(0),
  chain: text("chain").notNull(),
  hash_state: text("hash_state"),
  checked_bytes: integer("checked_bytes").notNull().default(0),
  verified: integer("verified").notNull().default(0),
}, (table) => [
  primaryKey({columns: [table.namespace, table.import_id, table.table_name]})
]);

export const ms_imports = sqliteTable("ms_imports", {
  namespace: text("namespace").notNull(),
  import_id: text("import_id").notNull(),
  manifest: text("manifest").notNull(),
  status: text("status").notNull().default("staging"),
  created_at: text("created_at").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.import_id]}),
  check("ms_imports_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_objects = sqliteTable("ms_objects", {
  namespace: text("namespace").notNull(),
  sha256: text("sha256").notNull(),
  bytes: integer("bytes").notNull(),
  descriptor: text("descriptor").notNull(),
  cursor: integer("cursor").notNull().default(0),
  hash_state: text("hash_state").notNull(),
  verified: integer("verified").notNull().default(0),
  valid_utf8: integer("valid_utf8").notNull().default(0),
  utf8_state: text("utf8_state").notNull().default("{\"needed\":0,\"min\":128,\"max\":191,\"valid\":true}"),
}, (table) => [
  primaryKey({columns: [table.namespace, table.sha256]}),
  check("ms_objects_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_operation_stages = sqliteTable("ms_operation_stages", {
  namespace: text("namespace").notNull(),
  operation_id: text("operation_id").notNull(),
  expected_revision: integer("expected_revision").notNull(),
  import_id: text("import_id").notNull(),
  mutation_count: integer("mutation_count").notNull(),
  file_count: integer("file_count").notNull(),
  expected_chain: text("expected_chain").notNull(),
  watermarks: text("watermarks").notNull(),
  payload_sha256: text("payload_sha256").notNull(),
  status: text("status").notNull().default("staging"),
  cursor: text("cursor").notNull().default(""),
  chain: text("chain").notNull(),
  checked_count: integer("checked_count").notNull().default(0),
}, (table) => [
  primaryKey({columns: [table.namespace, table.operation_id]})
]);

export const ms_operations = sqliteTable("ms_operations", {
  namespace: text("namespace").notNull(),
  operation_id: text("operation_id").notNull(),
  payload_sha256: text("payload_sha256").notNull(),
  revision: integer("revision").notNull(),
  noop: integer("noop").notNull(),
  created_at: text("created_at").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.operation_id]}),
  check("ms_operations_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_records = sqliteTable("ms_records", {
  namespace: text("namespace").notNull(),
  record_sha256: text("record_sha256").notNull(),
  envelope: text("envelope").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.record_sha256]}),
  check("ms_records_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_revisions = sqliteTable("ms_revisions", {
  namespace: text("namespace").notNull(),
  revision: integer("revision").notNull(),
  import_id: text("import_id").notNull(),
  operation_id: text("operation_id").notNull(),
  payload_sha256: text("payload_sha256").notNull(),
  watermarks: text("watermarks").notNull(),
  created_at: text("created_at").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.revision]}),
  unique("ms_revisions_namespace_operation_id_unique").on(table.namespace, table.operation_id),
  check("ms_revisions_namespace_check", sql`${table.namespace} = 'owner'`)
]);

export const ms_rows = sqliteTable("ms_rows", {
  namespace: text("namespace").notNull(),
  import_id: text("import_id").notNull(),
  table_name: text("table_name").notNull(),
  key_sha256: text("key_sha256").notNull(),
  revision: integer("revision").notNull(),
  record_sha256: text("record_sha256"),
  identity: text("identity"),
  run_id: text("run_id"),
  status: text("status"),
  task_type: text("task_type"),
  profile_hash: text("profile_hash"),
  provider: text("provider"),
  updated_at: text("updated_at"),
  sequence_value: integer("sequence_value"),
  projection: text("projection").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.import_id, table.table_name, table.key_sha256, table.revision]}),
  index("ms_rows_status").on(table.namespace, table.import_id, table.table_name, table.status, table.revision),
  index("ms_rows_runs").on(table.namespace, table.import_id, table.table_name, table.run_id, table.revision),
  index("ms_rows_lookup").on(table.namespace, table.import_id, table.table_name, table.identity, table.revision)
]);

export const ms_stage_batches = sqliteTable("ms_stage_batches", {
  namespace: text("namespace").notNull(),
  operation_id: text("operation_id").notNull(),
  batch_id: text("batch_id").notNull(),
  payload_sha256: text("payload_sha256").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.operation_id, table.batch_id]})
]);

export const ms_staged_items = sqliteTable("ms_staged_items", {
  namespace: text("namespace").notNull(),
  operation_id: text("operation_id").notNull(),
  item_key: text("item_key").notNull(),
  kind: text("kind").notNull(),
  item_sha256: text("item_sha256").notNull(),
  payload: text("payload").notNull(),
  normalized: text("normalized").notNull(),
}, (table) => [
  primaryKey({columns: [table.namespace, table.operation_id, table.item_key]})
]);
