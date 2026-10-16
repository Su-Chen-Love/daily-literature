CREATE TABLE `ms_chunks` (
	`namespace` text NOT NULL,
	`sha256` text NOT NULL,
	`bytes` integer NOT NULL,
	`data_base64` text NOT NULL,
	PRIMARY KEY(`namespace`, `sha256`),
	CONSTRAINT "ms_chunks_namespace_check" CHECK("ms_chunks"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE TABLE `ms_files` (
	`namespace` text NOT NULL,
	`import_id` text NOT NULL,
	`path` text NOT NULL,
	`revision` integer NOT NULL,
	`sha256` text NOT NULL,
	`bytes` integer NOT NULL,
	`mode` integer NOT NULL,
	PRIMARY KEY(`namespace`, `import_id`, `path`, `revision`)
);
--> statement-breakpoint
CREATE TABLE `ms_heads` (
	`namespace` text PRIMARY KEY,
	`revision` integer DEFAULT 0 NOT NULL,
	`import_id` text,
	`operation_id` text,
	`updated_at` text,
	CONSTRAINT "ms_heads_namespace_check" CHECK("ms_heads"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE TABLE `ms_import_batches` (
	`namespace` text NOT NULL,
	`import_id` text NOT NULL,
	`operation_id` text NOT NULL,
	`payload_sha256` text NOT NULL,
	PRIMARY KEY(`namespace`, `import_id`, `operation_id`)
);
--> statement-breakpoint
CREATE TABLE `ms_import_tables` (
	`namespace` text NOT NULL,
	`import_id` text NOT NULL,
	`table_name` text NOT NULL,
	`row_count` integer NOT NULL,
	`expected_chain` text NOT NULL,
	`metadata` text NOT NULL,
	`cursor` text DEFAULT '' NOT NULL,
	`checked_count` integer DEFAULT 0 NOT NULL,
	`chain` text NOT NULL,
	`hash_state` text,
	`checked_bytes` integer DEFAULT 0 NOT NULL,
	`verified` integer DEFAULT 0 NOT NULL,
	PRIMARY KEY(`namespace`, `import_id`, `table_name`)
);
--> statement-breakpoint
CREATE TABLE `ms_imports` (
	`namespace` text NOT NULL,
	`import_id` text NOT NULL,
	`manifest` text NOT NULL,
	`status` text DEFAULT 'staging' NOT NULL,
	`created_at` text NOT NULL,
	PRIMARY KEY(`namespace`, `import_id`),
	CONSTRAINT "ms_imports_namespace_check" CHECK("ms_imports"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE TABLE `ms_objects` (
	`namespace` text NOT NULL,
	`sha256` text NOT NULL,
	`bytes` integer NOT NULL,
	`descriptor` text NOT NULL,
	`cursor` integer DEFAULT 0 NOT NULL,
	`hash_state` text NOT NULL,
	`verified` integer DEFAULT 0 NOT NULL,
	`valid_utf8` integer DEFAULT 0 NOT NULL,
	`utf8_state` text DEFAULT '{"needed":0,"min":128,"max":191,"valid":true}' NOT NULL,
	PRIMARY KEY(`namespace`, `sha256`),
	CONSTRAINT "ms_objects_namespace_check" CHECK("ms_objects"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE TABLE `ms_operation_stages` (
	`namespace` text NOT NULL,
	`operation_id` text NOT NULL,
	`expected_revision` integer NOT NULL,
	`import_id` text NOT NULL,
	`mutation_count` integer NOT NULL,
	`file_count` integer NOT NULL,
	`expected_chain` text NOT NULL,
	`watermarks` text NOT NULL,
	`payload_sha256` text NOT NULL,
	`status` text DEFAULT 'staging' NOT NULL,
	`cursor` text DEFAULT '' NOT NULL,
	`chain` text NOT NULL,
	`checked_count` integer DEFAULT 0 NOT NULL,
	PRIMARY KEY(`namespace`, `operation_id`)
);
--> statement-breakpoint
CREATE TABLE `ms_operations` (
	`namespace` text NOT NULL,
	`operation_id` text NOT NULL,
	`payload_sha256` text NOT NULL,
	`revision` integer NOT NULL,
	`noop` integer NOT NULL,
	`created_at` text NOT NULL,
	PRIMARY KEY(`namespace`, `operation_id`),
	CONSTRAINT "ms_operations_namespace_check" CHECK("ms_operations"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE TABLE `ms_records` (
	`namespace` text NOT NULL,
	`record_sha256` text NOT NULL,
	`envelope` text NOT NULL,
	PRIMARY KEY(`namespace`, `record_sha256`),
	CONSTRAINT "ms_records_namespace_check" CHECK("ms_records"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE TABLE `ms_revisions` (
	`namespace` text NOT NULL,
	`revision` integer NOT NULL,
	`import_id` text NOT NULL,
	`operation_id` text NOT NULL,
	`payload_sha256` text NOT NULL,
	`watermarks` text NOT NULL,
	`created_at` text NOT NULL,
	PRIMARY KEY(`namespace`, `revision`),
	CONSTRAINT "ms_revisions_namespace_check" CHECK("ms_revisions"."namespace" = 'owner')
);
--> statement-breakpoint
CREATE UNIQUE INDEX `ms_revisions_namespace_operation_id_unique` ON `ms_revisions` (`namespace`,`operation_id`);--> statement-breakpoint
CREATE TABLE `ms_rows` (
	`namespace` text NOT NULL,
	`import_id` text NOT NULL,
	`table_name` text NOT NULL,
	`key_sha256` text NOT NULL,
	`revision` integer NOT NULL,
	`record_sha256` text,
	`identity` text,
	`run_id` text,
	`status` text,
	`task_type` text,
	`profile_hash` text,
	`provider` text,
	`updated_at` text,
	`sequence_value` integer,
	`projection` text NOT NULL,
	PRIMARY KEY(`namespace`, `import_id`, `table_name`, `key_sha256`, `revision`)
);
--> statement-breakpoint
CREATE INDEX `ms_rows_status` ON `ms_rows` (`namespace`,`import_id`,`table_name`,`status`,`revision`);--> statement-breakpoint
CREATE INDEX `ms_rows_runs` ON `ms_rows` (`namespace`,`import_id`,`table_name`,`run_id`,`revision`);--> statement-breakpoint
CREATE INDEX `ms_rows_lookup` ON `ms_rows` (`namespace`,`import_id`,`table_name`,`identity`,`revision`);--> statement-breakpoint
CREATE TABLE `ms_stage_batches` (
	`namespace` text NOT NULL,
	`operation_id` text NOT NULL,
	`batch_id` text NOT NULL,
	`payload_sha256` text NOT NULL,
	PRIMARY KEY(`namespace`, `operation_id`, `batch_id`)
);
--> statement-breakpoint
CREATE TABLE `ms_staged_items` (
	`namespace` text NOT NULL,
	`operation_id` text NOT NULL,
	`item_key` text NOT NULL,
	`kind` text NOT NULL,
	`item_sha256` text NOT NULL,
	`payload` text NOT NULL,
	`normalized` text NOT NULL,
	PRIMARY KEY(`namespace`, `operation_id`, `item_key`)
);

--> statement-breakpoint
INSERT OR IGNORE INTO ms_heads(namespace, revision) VALUES ('owner', 0);
