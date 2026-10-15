-- Immutable cloud-owner edits and their cursor are committed with profile state.
CREATE TABLE IF NOT EXISTS applied_cloud_profile_events(
  seq INTEGER PRIMARY KEY,
  event_json TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
