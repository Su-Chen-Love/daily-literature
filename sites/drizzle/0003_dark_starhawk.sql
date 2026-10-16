CREATE TABLE `display_generations` (
	`generation` text PRIMARY KEY NOT NULL
);
--> statement-breakpoint
CREATE TABLE `display_payloads` (
	`checksum` text PRIMARY KEY NOT NULL,
	`data` text NOT NULL
);
