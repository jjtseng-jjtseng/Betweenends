"""Per-archer profiles, season history and trends, built from the normalised tables.

    python -m pipeline.archers [--as-of YYYY-MM-DD]

Reads  data/processed/{tournaments,events,entries,match_rows}.csv.gz
Writes data/processed/
    archers.csv.gz           one row per archer: who they are, when/where/what they shoot
    archer_seasons.csv.gz    archer x year x division x format_family performance summary
    archer_trends.csv.gz     archer x division x format_family: trajectory over time
    marquee_results.csv.gz   every archer's result at nationals / USAT / team trials
    yearly_summary.csv       participation + scoring level per year and tournament category

Conventions
-----------
* Only **canonical** performances are used (``entries.is_canonical``): when the same
  physical performance appears in a site event and in a Combined-results event it is
  counted once.
* Scoring statistics use **complete cards** only (every regulation arrow shot) and
  the regulation score (shoot-off arrows excluded), so a partial card from a
  withdrawn archer never drags an average down.
* ``avg_arrow`` (points per arrow) makes performances over different numbers of
  arrows comparable *within the same format_family*; do not compare across families
  (e.g. indoor 10-zone vs 3D).
* Guest entries (not eligible for titles) are kept but flagged; trend tables exclude them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

MIN_PERFS_FOR_TREND = 3


def _mode(s: pd.Series):
    s = s.dropna()
    s = s[s.astype(str) != ""]
    return Counter(s).most_common(1)[0][0] if len(s) else ""


def _top(s: pd.Series, n: int = 3) -> str:
    s = s.dropna()
    s = s[s.astype(str).str.strip() != ""]
    return "|".join(k for k, _ in Counter(s).most_common(n))


def _ols(x: np.ndarray, y: np.ndarray):
    """Slope, standard error of slope, r^2 of y ~ x. Needs n >= 3 and varying x."""
    n = len(x)
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    if n < 3 or sxx == 0:
        return float("nan"), float("nan"), float("nan")
    slope = ((x - xm) * (y - ym)).sum() / sxx
    resid = y - (ym + slope * (x - xm))
    sse = (resid ** 2).sum()
    se = math.sqrt(sse / (n - 2) / sxx) if n > 2 else float("nan")
    sst = ((y - ym) ** 2).sum()
    r2 = 1 - sse / sst if sst > 0 else float("nan")
    return float(slope), float(se), float(r2)


def _trend_label(slope: float, se: float, n: int) -> str:
    if n < MIN_PERFS_FOR_TREND or math.isnan(slope):
        return "insufficient_data"
    if se is None or math.isnan(se) or se == 0:
        return "improving" if slope > 0 else "declining" if slope < 0 else "stable"
    t = slope / se
    if t >= 2:
        return "improving"
    if t <= -2:
        return "declining"
    return "stable"



_NATIONAL_CATS = {"indoor_nationals", "joad_indoor_nationals", "target_nationals", "joad_target_nationals",
                  "collegiate_target_nationals", "collegiate_3d_nationals", "field_nationals", "3d_nationals", "oas_nationals"}
_FAMILY_BY_SEASON = {"Indoor": "indoor_wa", "Outdoor": "outdoor_wa", "Field": "field_wa"}


def _legacy_performances(proc: Path) -> pd.DataFrame:
    """Pre-Dec-2017 results parsed from USA Archery PDFs, in the same schema as Between Ends entries."""
    le_p, lt_p = proc / "legacy_entries.csv.gz", proc / "legacy_tournaments.csv"
    if not (le_p.exists() and lt_p.exists()):
        return pd.DataFrame()
    le = pd.read_csv(le_p, low_memory=False)
    lt = pd.read_csv(lt_p)
    lt = lt[lt.parsed][["tournament_key", "title", "category", "start_date", "year", "date_source"]]
    x = le.merge(lt, on="tournament_key", how="inner", suffixes=("", "_t"))
    if x.empty:
        return pd.DataFrame()
    x["date_approx"] = x.start_date.isna()
    x["start_date"] = pd.to_datetime(x.start_date.fillna(x.year.astype("Int64").astype(str) + "-07-01"))
    grp = x.groupby(["tournament_key", "season", "division_raw", "class_raw", "gender_raw", "round_desc"], dropna=False)["rank"]
    x["group_size"] = grp.transform("size")
    out = pd.DataFrame(
        {
            "source": "usarchery_pdf", "archer_id": x.archer_id, "display_name": x.display_name, "alt_id": "",
            "identity_basis": x.identity_basis, "entry_id": x.entry_id, "tournament_id": x.tournament_key,
            "name": x.title, "start_date": x.start_date, "date_approx": x.date_approx, "year": x.year,
            "category": x.category, "championship_key": [f"{c}:{y}" if c in _NATIONAL_CATS or c == "usat_series" else None
                                                         for c, y in zip(x.category, x.year)],
            "edition": None, "usat_leg": None, "event_id": x.tournament_key, "event_name": x.round_desc, "event_type": "LegacyPDF",
            "division": x.division, "age_class": x.age_class, "gender": x.gender, "club": x.club.fillna(""),
            "is_guest": False, "is_collegiate": x.is_collegiate, "is_para": x.is_para,
            "total_score": x.total_score, "regulation_score": x.total_score, "regulation_arrows": x.regulation_arrows,
            "reg_arrows_shot": x.regulation_arrows, "complete_card": x.complete_card.fillna(False).astype(bool),
            "avg_arrow": x.avg_arrow, "pct_of_max": x.pct_of_max, "x_count": float("nan"), "ten_count": float("nan"),
            "rank_primary": x["rank"], "group_size_primary": x.group_size,
            "percentile_primary": (1 - (x["rank"] - 1) / x.group_size).round(4),
            "format_family": x.season.map(_FAMILY_BY_SEASON).fillna("other"),
            "is_canonical": True, "is_national_championship": x.category.isin(_NATIONAL_CATS),
            "is_indoor_national": x.category.isin({"indoor_nationals", "joad_indoor_nationals"}),
            "is_outdoor_national": x.category.isin(_NATIONAL_CATS - {"indoor_nationals", "joad_indoor_nationals"}),
            "is_usat": x.category.eq("usat_series"), "is_team_trials": x.category.eq("team_trials"),
            "is_marquee": x.category.isin(_NATIONAL_CATS | {"usat_series", "team_trials"}),
        }
    )
    return out


def load(proc: Path):
    t = pd.read_csv(proc / "tournaments.csv.gz", dtype={"usat_leg": str})
    ev = pd.read_csv(proc / "events.csv.gz")
    en = pd.read_csv(proc / "entries.csv.gz", dtype={"alt_id": str, "alt_raw": str, "perf_key": str}, low_memory=False)
    mt_path = proc / "match_rows.csv.gz"
    mt = pd.read_csv(mt_path, low_memory=False) if mt_path.exists() else pd.DataFrame()
    return t, ev, en, mt


def build(proc: Path, as_of: dt.date) -> dict:
    t, ev, en, mt = load(proc)
    if en.empty:
        raise SystemExit("entries.csv.gz is empty - run collect + normalize first")

    tcols = ["tournament_id", "name", "start_date", "year", "category", "is_national_championship", "is_indoor_national",
             "is_outdoor_national", "is_usat", "is_team_trials", "is_marquee", "championship_key", "edition", "usat_leg"]
    ecols = ["event_id", "format_family", "format_key", "scoring_rule", "event_name"]
    d = (en.merge(t[tcols], on="tournament_id", how="left")
           .merge(ev[ecols], on="event_id", how="left"))
    d["start_date"] = pd.to_datetime(d.start_date)
    d["source"] = "betweenends"
    d["date_approx"] = False
    d["tournament_id"] = d.tournament_id.astype(str)          # unify with legacy string keys
    if not mt.empty:
        mt["tournament_id"] = mt.tournament_id.astype(str)
    lg = _legacy_performances(proc)
    if len(lg):
        d = pd.concat([d, lg], ignore_index=True)
        print(f"  + {len(lg):,} legacy (2016-2017, usarchery.org PDF) performances")
    can = d[d.is_canonical].copy()

    # ------------------------------------------------------------------ archers
    g = can.groupby("archer_id")
    arch = pd.DataFrame(
        {
            "display_name": g.display_name.agg(_mode),
            "alt_id": g.alt_id.agg(_mode),
            "alt_ids": g.alt_id.agg(lambda s: "|".join(sorted({str(x) for x in s if str(x) not in ("", "nan")}))),
            "identity_basis": g.identity_basis.agg(lambda s: "alt" if (s == "alt").all() else _mode(s)),
            "name_variants": g.display_name.agg(lambda s: "|".join(sorted(set(s))[:6])),
            "gender": g.gender.agg(_mode),
            "primary_division": g.division.agg(_mode),
            "divisions": g.division.agg(lambda s: "|".join(sorted(set(s)))),
            "first_date": g.start_date.min().dt.date,
            "last_date": g.start_date.max().dt.date,
            "n_entries": g.size(),
            "n_tournaments": g.tournament_id.nunique(),
            "active_years": g.year.agg(lambda s: "|".join(str(int(y)) for y in sorted(set(s.dropna())))),
            "n_active_years": g.year.nunique(),
            "top_clubs": g.club.agg(_top),
        }
    )
    latest = can.sort_values("start_date").groupby("archer_id").tail(1).set_index("archer_id")
    arch["latest_club"] = latest.club
    arch["latest_age_class"] = latest.age_class
    arch["latest_division"] = latest.division
    arch["age_classes"] = g.age_class.agg(lambda s: "|".join(sorted(set(s))))
    # marquee participation
    mq = can[can.is_marquee]
    arch["n_marquee_tournaments"] = mq.groupby("archer_id").tournament_id.nunique()
    arch["n_national_championships"] = can[can.is_national_championship].groupby("archer_id").championship_key.nunique()
    arch["n_usat_events"] = can[can.is_usat].groupby("archer_id").tournament_id.nunique()
    arch["n_team_trials"] = can[can.is_team_trials].groupby("archer_id").tournament_id.nunique()
    for c in ("n_marquee_tournaments", "n_national_championships", "n_usat_events", "n_team_trials"):
        arch[c] = arch[c].fillna(0).astype(int)
    cutoff = pd.Timestamp(as_of) - pd.DateOffset(months=12)
    arch["active_last_12_months"] = pd.to_datetime(arch.last_date) >= cutoff
    srcs = can.groupby("archer_id").source.agg(lambda s: "|".join(sorted(set(s))))
    arch["sources"] = srcs
    arch = arch.reset_index().sort_values(["n_entries", "archer_id"], ascending=[False, True]).reset_index(drop=True)

    # --------------------------------------------------------- scoring population
    sc = can[(can.complete_card) & can.avg_arrow.notna() & (~can.is_guest)].copy()
    sc["year"] = sc.start_date.dt.year

    # ------------------------------------------------------------------ seasons
    sg = sc.groupby(["archer_id", "year", "division", "format_family"])
    seasons = sg.agg(
        n_performances=("entry_id", "size"),
        mean_avg_arrow=("avg_arrow", "mean"),
        best_avg_arrow=("avg_arrow", "max"),
        mean_pct_of_max=("pct_of_max", "mean"),
        best_regulation_score=("regulation_score", "max"),
        mean_percentile=("percentile_primary", "mean"),
        best_rank=("rank_primary", "min"),
        total_x=("x_count", "sum"),
        total_tens=("ten_count", "sum"),
        total_arrows=("reg_arrows_shot", "sum"),
    ).reset_index()
    seasons["ten_rate"] = (seasons.total_tens / seasons.total_arrows).round(4)
    seasons["x_rate"] = (seasons.total_x / seasons.total_arrows).round(4)
    for c in ("mean_avg_arrow", "best_avg_arrow", "mean_pct_of_max", "mean_percentile"):
        seasons[c] = seasons[c].round(4)

    # ------------------------------------------------------------------- trends
    rows = []
    for (aid, div, fam), grp in sc.groupby(["archer_id", "division", "format_family"]):
        grp = grp.sort_values("start_date")
        n = len(grp)
        x = ((grp.start_date - grp.start_date.min()).dt.days / 365.25).to_numpy(dtype=float)
        y = grp.avg_arrow.to_numpy(dtype=float)
        slope, se, r2 = _ols(x, y) if n >= MIN_PERFS_FOR_TREND else (float("nan"),) * 3
        recent = y[-3:].mean()
        prior = y[:-3].mean() if n > 3 else float("nan")
        rows.append(
            {
                "archer_id": aid, "division": div, "format_family": fam, "n_performances": n,
                "first_date": grp.start_date.iloc[0].date(), "last_date": grp.start_date.iloc[-1].date(),
                "span_years": round(float(x[-1]), 2),
                "first_avg_arrow": round(float(y[0]), 4), "last_avg_arrow": round(float(y[-1]), 4),
                "best_avg_arrow": round(float(y.max()), 4), "mean_avg_arrow": round(float(y.mean()), 4),
                "sd_avg_arrow": round(float(y.std(ddof=1)), 4) if n > 1 else None,
                "slope_per_year": round(slope, 4) if not math.isnan(slope) else None,
                "slope_se": round(se, 4) if not math.isnan(se) else None,
                "r2": round(r2, 3) if not math.isnan(r2) else None,
                "recent3_mean": round(float(recent), 4),
                "prior_mean": round(float(prior), 4) if not math.isnan(prior) else None,
                "recent_vs_prior": round(float(recent - prior), 4) if not math.isnan(prior) else None,
                "trend": _trend_label(slope, se, n),
                "best_pct_of_max": round(float(grp.pct_of_max.max()), 4),
                "last_pct_of_max": round(float(grp.pct_of_max.iloc[-1]), 4),
            }
        )
    trends = pd.DataFrame(rows).sort_values(["archer_id", "division", "format_family"]).reset_index(drop=True)

    # ----------------------------------------------------------- marquee results
    mres = can[can.is_marquee].copy()
    keep = ["source", "date_approx", "archer_id", "display_name", "tournament_id", "name", "start_date", "year", "category", "championship_key",
            "edition", "usat_leg", "event_id", "event_name", "event_type", "division", "age_class", "gender", "is_guest",
            "club", "total_score", "regulation_score", "regulation_arrows", "complete_card", "avg_arrow", "pct_of_max",
            "x_count", "ten_count", "rank_primary", "group_size_primary", "percentile_primary"]
    mres = mres[keep].rename(columns={"name": "tournament_name", "rank_primary": "qualification_rank",
                                      "group_size_primary": "group_size", "percentile_primary": "percentile"})
    if not mt.empty and "final_place" in mt.columns:
        fp = (mt[mt.final_place.notna() & (mt.archer_id.fillna("") != "")]
              .groupby(["tournament_id", "archer_id", "division", "age_class", "gender"], as_index=False).final_place.min())
        mres = mres.merge(fp, on=["tournament_id", "archer_id", "division", "age_class", "gender"], how="left")
    else:
        mres["final_place"] = np.nan
    mres["start_date"] = mres.start_date.dt.date
    mres = mres.sort_values(["start_date", "tournament_id", "division", "gender", "qualification_rank"]).reset_index(drop=True)

    # ------------------------------------------------------------ yearly summary
    can["_first_year"] = can.groupby("archer_id").year.transform("min")
    ys = can.groupby(["year", "category"]).agg(
        tournaments=("tournament_id", "nunique"), entries=("entry_id", "size"), unique_archers=("archer_id", "nunique"),
        new_archers=("archer_id", lambda s: s[can.loc[s.index, "_first_year"] == can.loc[s.index, "year"]].nunique()),
    ).reset_index()
    lvl = sc.groupby(["year", "category"]).agg(mean_pct_of_max=("pct_of_max", "mean"),
                                               complete_cards=("entry_id", "size")).reset_index()
    ys = ys.merge(lvl, on=["year", "category"], how="left")
    ys["mean_pct_of_max"] = ys.mean_pct_of_max.round(4)
    ys = ys.sort_values(["year", "category"]).reset_index(drop=True)

    # ------------------------------------------------------------------- write
    def w(df, name, gz=True):
        p = proc / name
        if gz:
            df.to_csv(p, index=False, compression={"method": "gzip", "mtime": 0, "compresslevel": 6}, lineterminator="\n")
        else:
            df.to_csv(p, index=False, lineterminator="\n")
        print(f"  wrote {name:<28} rows={len(df):>8,}  {p.stat().st_size / 1e6:6.2f} MB")

    print("writing archer tables ...")
    w(arch, "archers.csv.gz")
    w(seasons, "archer_seasons.csv.gz")
    w(trends, "archer_trends.csv.gz")
    w(mres, "marquee_results.csv.gz")
    w(ys, "yearly_summary.csv", gz=False)
    return {"archers": len(arch), "seasons": len(seasons), "trends": len(trends), "marquee_results": len(mres)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline.archers", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    p.add_argument("--dir", type=Path, default=config.PROCESSED_DIR)
    a = p.parse_args(argv)
    print(build(a.dir, a.as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
