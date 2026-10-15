CREATE TABLE `cloud_profile_events` (
	`seq` integer PRIMARY KEY NOT NULL,
	`base_version` integer NOT NULL,
	`base_generation` text NOT NULL,
	`base_profile_hash` text NOT NULL,
	`base_preferences` text NOT NULL,
	`profile_hash` text NOT NULL,
	`content` text NOT NULL,
	`screening_preferences` text NOT NULL,
	`summary` text NOT NULL,
	`updated_at` text NOT NULL
);
