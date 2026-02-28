PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  kickoff_utc TEXT NOT NULL,
  league TEXT NOT NULL,
  season TEXT,
  home_team TEXT NOT NULL,
  away_team TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_corners_form (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_utc TEXT NOT NULL,
  league TEXT NOT NULL,
  season TEXT,
  team TEXT NOT NULL,
  venue TEXT NOT NULL CHECK (venue IN ('home','away')),
  n_matches INTEGER NOT NULL,
  cf_mean REAL NOT NULL,
  ca_mean REAL NOT NULL,
  cf_var REAL,
  ca_var REAL,
  UNIQUE(as_of_utc, league, season, team, venue)
);

CREATE TABLE IF NOT EXISTS odds_corners_ou (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  captured_utc TEXT NOT NULL,
  match_id TEXT NOT NULL,
  line REAL NOT NULL,
  over_odds REAL NOT NULL,
  under_odds REAL NOT NULL,
  book TEXT,
  UNIQUE(captured_utc, match_id, line, book),
  FOREIGN KEY(match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS model_corners_ou (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_utc TEXT NOT NULL,
  match_id TEXT NOT NULL,
  line REAL NOT NULL,
  mu_total REAL NOT NULL,
  k_disp REAL NOT NULL,
  p_over REAL NOT NULL,
  p_under REAL NOT NULL,
  ev_over REAL NOT NULL,
  ev_under REAL NOT NULL,
  bet_score_over REAL NOT NULL,
  bet_score_under REAL NOT NULL,
  flags TEXT,
  FOREIGN KEY(match_id) REFERENCES matches(match_id),
  UNIQUE(run_utc, match_id, line)
);

CREATE TABLE IF NOT EXISTS bets (
  bet_id TEXT PRIMARY KEY,
  placed_utc TEXT NOT NULL,
  match_id TEXT NOT NULL,
  market TEXT NOT NULL,
  selection TEXT NOT NULL,
  line REAL NOT NULL,
  odds REAL NOT NULL,
  stake REAL NOT NULL,
  model_p REAL NOT NULL,
  model_ev REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','SETTLED','VOID')),
  profit REAL DEFAULT 0.0,
  notes TEXT,
  FOREIGN KEY(match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS results (
  match_id TEXT PRIMARY KEY,
  final_corners_total INTEGER,
  final_corners_home INTEGER,
  final_corners_away INTEGER,
  result_utc TEXT,
  FOREIGN KEY(match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_odds_match_time ON odds_corners_ou(match_id, captured_utc);
CREATE INDEX IF NOT EXISTS idx_model_match_time ON model_corners_ou(match_id, run_utc);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
