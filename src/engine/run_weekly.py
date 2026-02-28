import csv
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from scipy.stats import nbinom

DB_PATH = Path("db/betting.sqlite")
FIXTURES_CSV = Path("data/raw/fixtures.csv")
ODDS_CSV = Path("data/raw/odds_corners.csv")
HISTORY_CSV = Path("data/raw/corners_history.csv")

EV_THRESHOLD = 0.03  # 3%


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_fixtures(conn: sqlite3.Connection) -> int:
    if not FIXTURES_CSV.exists():
        print(f"Fixtures file not found: {FIXTURES_CSV}")
        return 0

    with FIXTURES_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    cursor = conn.cursor()
    for r in rows:
        cursor.execute(
            """
            INSERT OR REPLACE INTO matches
            (match_id, kickoff_utc, league, season, home_team, away_team)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                r["match_id"].strip(),
                r["kickoff_utc"].strip(),
                r["league"].strip(),
                (r.get("season") or "").strip() or None,
                r["home_team"].strip(),
                r["away_team"].strip(),
            ),
        )
    conn.commit()
    return len(rows)


def load_odds(conn: sqlite3.Connection) -> int:
    if not ODDS_CSV.exists():
        print(f"Odds file not found: {ODDS_CSV}")
        return 0

    with ODDS_CSV.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    cursor = conn.cursor()
    for r in rows:
        cursor.execute(
            """
            INSERT INTO odds_corners_ou
            (captured_utc, match_id, line, over_odds, under_odds, book)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                r["captured_utc"].strip(),
                r["match_id"].strip(),
                float(r["line"]),
                float(r["over_odds"]),
                float(r["under_odds"]),
                (r.get("book") or "").strip() or None,
            ),
        )
    conn.commit()
    return len(rows)


def ev(p: float, odds: float) -> float:
    # EV = p*(odds-1) - (1-p)
    return p * (odds - 1.0) - (1.0 - p)


def estimate_k_disp(total_corners: pd.Series) -> float:
    """
    Estimate NB dispersion k using method-of-moments:
      Var = mu + mu^2/k  =>  k = mu^2 / (Var - mu)
    If Var <= mu, we fallback to a large k (approx Poisson).
    """
    mu = float(total_corners.mean())
    var = float(total_corners.var(ddof=1)) if len(total_corners) > 1 else mu

    if var <= mu + 1e-9:
        return 1e6  # near-Poisson

    k = (mu * mu) / (var - mu)
    return max(k, 0.1)


def build_team_form(history: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a long table with team form by venue:
      team, venue(home/away), n_matches, cf_mean, ca_mean
    Where:
      cf = corners for, ca = corners against
    """
    # Home venue stats
    home = history.groupby(["league", "season", "home_team"], as_index=False).agg(
        n_matches=("corners_home", "count"),
        cf_mean=("corners_home", "mean"),
        ca_mean=("corners_away", "mean"),
        cf_var=("corners_home", "var"),
        ca_var=("corners_away", "var"),
    ).rename(columns={"home_team": "team"})
    home["venue"] = "home"

    # Away venue stats
    away = history.groupby(["league", "season", "away_team"], as_index=False).agg(
        n_matches=("corners_away", "count"),
        cf_mean=("corners_away", "mean"),
        ca_mean=("corners_home", "mean"),
        cf_var=("corners_away", "var"),
        ca_var=("corners_home", "var"),
    ).rename(columns={"away_team": "team"})
    away["venue"] = "away"

    out = pd.concat([home, away], ignore_index=True)
    # Replace NaN variances (when only 1 match) with None-ish
    out["cf_var"] = out["cf_var"].where(out["cf_var"].notna(), None)
    out["ca_var"] = out["ca_var"].where(out["ca_var"].notna(), None)
    return out


def upsert_team_form(conn: sqlite3.Connection, team_form: pd.DataFrame, as_of_utc: str):
    cursor = conn.cursor()
    for _, r in team_form.iterrows():
        cursor.execute(
            """
            INSERT OR REPLACE INTO team_corners_form
            (as_of_utc, league, season, team, venue, n_matches, cf_mean, ca_mean, cf_var, ca_var)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                as_of_utc,
                r["league"],
                r["season"],
                r["team"],
                r["venue"],
                int(r["n_matches"]),
                float(r["cf_mean"]),
                float(r["ca_mean"]),
                (float(r["cf_var"]) if r["cf_var"] is not None and pd.notna(r["cf_var"]) else None),
                (float(r["ca_var"]) if r["ca_var"] is not None and pd.notna(r["ca_var"]) else None),
            ),
        )
    conn.commit()


def get_form_lookup(team_form: pd.DataFrame):
    """
    Build dict: (league, season, team, venue) -> row
    """
    d = {}
    for _, r in team_form.iterrows():
        d[(r["league"], r["season"], r["team"], r["venue"])] = r
    return d


def nb_p_over(mu_total: float, k_disp: float, line: float) -> float:
    """
    Compute P(TotalCorners > line) for half-lines (e.g. 9.5).
    For line x.5: over means >= ceil(x+0.5) = x+1 if x is int.
    Example 9.5: over means >=10 => P(X >= 10) = 1 - CDF(9)
    So threshold = floor(line) for .5 lines.
    """
    threshold = int(line // 1)  # 9.5 -> 9, 10.5 -> 10

    # scipy nbinom parameterization:
    # mean = n*(1-p)/p with n=k
    # p = k/(k+mu)
    k = max(float(k_disp), 0.1)
    p = k / (k + float(mu_total))
    cdf = nbinom.cdf(threshold, k, p)  # P(X <= threshold)
    return float(1.0 - cdf)


def run_nb_model(conn: sqlite3.Connection) -> int:
    if not HISTORY_CSV.exists():
        print(f"History file not found: {HISTORY_CSV}")
        return 0

    history = pd.read_csv(HISTORY_CSV)
    # Ensure numeric
    history["corners_home"] = pd.to_numeric(history["corners_home"])
    history["corners_away"] = pd.to_numeric(history["corners_away"])
    history["total_corners"] = history["corners_home"] + history["corners_away"]

    run_utc = now_utc_iso()

    # Estimate league-level dispersion k (per league+season for now)
    # With tiny mock data, this is rough, but pipeline is correct.
    k_by_ls = (
        history.groupby(["league", "season"])["total_corners"]
        .apply(estimate_k_disp)
        .to_dict()
    )

    # Team form CF/CA home/away
    team_form = build_team_form(history)
    upsert_team_form(conn, team_form, as_of_utc=run_utc)
    lookup = get_form_lookup(team_form)

    cursor = conn.cursor()
    # Latest odds per match+line
    cursor.execute(
        """
        SELECT o.match_id, o.line, o.over_odds, o.under_odds, m.league, m.season, m.home_team, m.away_team
        FROM odds_corners_ou o
        JOIN matches m ON m.match_id = o.match_id
        JOIN (
          SELECT match_id, line, MAX(captured_utc) AS max_t
          FROM odds_corners_ou
          GROUP BY match_id, line
        ) latest
        ON o.match_id = latest.match_id AND o.line = latest.line AND o.captured_utc = latest.max_t
        ORDER BY o.match_id
        """
    )
    rows = cursor.fetchall()

    inserted = 0
    for match_id, line, over_odds, under_odds, league, season, home, away in rows:
        # Minimal sample filter (avoid garbage early)
        home_h = lookup.get((league, season, home, "home"))
        home_a = lookup.get((league, season, home, "away"))
        away_h = lookup.get((league, season, away, "home"))
        away_a = lookup.get((league, season, away, "away"))

        flags = {}
        # We need: home CF_home & CA_home; away CF_away & CA_away
        # If missing, flag and skip (in real feeds we’ll handle gracefully)
        if home_h is None or away_a is None or home_a is None or away_h is None:
            flags["missing_form"] = True

        # If missing form, we skip writing model output for now
        if flags.get("missing_form"):
            continue

        # Sample size checks
        if int(home_h["n_matches"]) < 2 or int(away_a["n_matches"]) < 2 or int(home_a["n_matches"]) < 2 or int(away_h["n_matches"]) < 2:
            flags["low_sample"] = True

        # Core mu_total
        mu_home = 0.5 * float(home_h["cf_mean"]) + 0.5 * float(away_a["ca_mean"])
        mu_away = 0.5 * float(away_a["cf_mean"]) + 0.5 * float(home_h["ca_mean"])  # away CF_away vs home CA_home
        mu_total = mu_home + mu_away

        k_disp = float(k_by_ls.get((league, season), 1e6))

        p_over = nb_p_over(mu_total, k_disp, float(line))
        p_under = 1.0 - p_over

        ev_over = ev(p_over, float(over_odds))
        ev_under = ev(p_under, float(under_odds))

        # For now bet_score = EV (we’ll upgrade to EV/uncertainty)
        bet_score_over = ev_over
        bet_score_under = ev_under

        flags_str = None if not flags else str(flags)

        cursor.execute(
            """
            INSERT OR REPLACE INTO model_corners_ou
            (run_utc, match_id, line, mu_total, k_disp, p_over, p_under,
             ev_over, ev_under, bet_score_over, bet_score_under, flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_utc, match_id, float(line), float(mu_total), float(k_disp),
                float(p_over), float(p_under),
                float(ev_over), float(ev_under),
                float(bet_score_over), float(bet_score_under),
                flags_str,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted


def print_value_picks(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT m.match_id, m.home_team, m.away_team, mo.line,
               mo.p_over, mo.ev_over, o.over_odds,
               mo.p_under, mo.ev_under, o.under_odds,
               mo.mu_total, mo.k_disp, mo.flags
        FROM model_corners_ou mo
        JOIN matches m ON m.match_id = mo.match_id
        JOIN odds_corners_ou o ON o.match_id = mo.match_id AND o.line = mo.line
        WHERE mo.run_utc = (SELECT MAX(run_utc) FROM model_corners_ou)
          AND o.captured_utc = (
             SELECT MAX(captured_utc) FROM odds_corners_ou oo
             WHERE oo.match_id = o.match_id AND oo.line = o.line
          )
        ORDER BY m.kickoff_utc
        """
    )
    rows = cursor.fetchall()

    print("\nCandidates (EV >= 3%):")
    for (match_id, home, away, line, p_over, ev_over, over_odds, p_under, ev_under, under_odds, mu_total, k_disp, flags) in rows:
        best = None
        if ev_over >= EV_THRESHOLD and ev_over >= ev_under:
            best = ("OVER", p_over, ev_over, over_odds)
        elif ev_under >= EV_THRESHOLD and ev_under > ev_over:
            best = ("UNDER", p_under, ev_under, under_odds)

        if best:
            side, p, evv, odds = best
            print(f"- {match_id} {home} vs {away} | {side} {line} | odds={odds:.2f} | p={p:.3f} | EV={evv:.3%} | mu={mu_total:.2f} k={k_disp:.2f} flags={flags}")


def main():
    if not DB_PATH.exists():
        print("Database not found.")
        return

    conn = sqlite3.connect(DB_PATH)

    n_fix = load_fixtures(conn)
    n_odds = load_odds(conn)
    n_model = run_nb_model(conn)

    print(f"Loaded fixtures: {n_fix}")
    print(f"Loaded odds rows: {n_odds}")
    print(f"Model rows written: {n_model}")

    if n_model > 0:
        print_value_picks(conn)
    else:
        print("\nNo model rows written (likely missing history/form).")

    conn.close()


if __name__ == "__main__":
    main()
