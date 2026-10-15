import { integer, sqliteTable, text, primaryKey } from "drizzle-orm/sqlite-core";
export const generations = sqliteTable("generations", {
  id: text("id").primaryKey(), count: integer("count").notNull(), createdAt: text("created_at").notNull(),
  status: text("status").notNull().default("staging"),
});
export const records = sqliteTable("records", {
  generation: text("generation").notNull(), kind: text("kind").notNull(), key: text("key").notNull(),
  data: text("data").notNull(), checksum: text("checksum").notNull(),
}, (table) => [primaryKey({columns: [table.generation, table.kind, table.key]})]);
export const pointers = sqliteTable("pointers", { key: text("key").primaryKey(), value: text("value").notNull() });
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
