import re
from collections import defaultdict
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.dependencies import RedirectIfNotAuthenticated
from app.database import get_db
from app.models.competitor import Competitor, CompetitorAttorney
from app.models.filings import FilingSnapshot
from app.services.pacer_discovery import get_cached_results

_SUFFIX_RE = re.compile(
    r',?\s*(Jr\.?|Sr\.?|II|III|IV|V|Esq\.?|P\.A\.?|PA)$', re.IGNORECASE
)

def _last_name(full_name: str) -> str:
    """Extract normalised last name, stripping professional suffixes first."""
    name = _SUFFIX_RE.sub("", full_name).strip().rstrip(",")
    parts = name.split()
    return parts[-1].lower().rstrip(",.") if parts else ""

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
auth_required = RedirectIfNotAuthenticated()


@router.get("/filings", response_class=HTMLResponse)
def filings(
    request: Request,
    user: dict = Depends(auth_required),
    db: Session = Depends(get_db),
    win: str = Query("1m"),
):
    has_data = db.query(FilingSnapshot).first() is not None

    if not has_data:
        return templates.TemplateResponse("filings.html", {
            "request": request,
            "user": user,
            "active_page": "filings",
            "has_data": False,
        })

    all_snapshots = db.query(FilingSnapshot).all()

    attorney_map = {a.id: a for a in db.query(CompetitorAttorney).all()}
    comp_map = {
        c.id: c
        for c in db.query(Competitor)
        .filter(Competitor.active == True)
        .options(joinedload(Competitor.locations), joinedload(Competitor.attorneys))
        .all()
    }

    # De-dupe: max case_count per (competitor_id, attorney_id, district, chapter, period_start)
    counts: dict = {}
    for s in all_snapshots:
        key = (s.competitor_id, s.attorney_id, s.district, s.chapter, s.period_start)
        if key not in counts or s.case_count > counts[key]:
            counts[key] = s.case_count

    # All distinct periods, newest first
    distinct_periods = sorted(set(per for (_, _, _, _, per) in counts), reverse=True)

    # Validate + resolve window slice
    if win not in {"1m", "3m", "6m", "12m", "all"}:
        win = "1m"
    win_slices = {
        "1m":  (distinct_periods[:1],  distinct_periods[1:2]),
        "3m":  (distinct_periods[:3],  distinct_periods[3:6]),
        "6m":  (distinct_periods[:6],  distinct_periods[6:12]),
        "12m": (distinct_periods[:12], distinct_periods[12:24]),
        "all": (distinct_periods[:],   []),
    }
    incl_periods, cmp_periods = win_slices[win]

    def _period_label(periods):
        if not periods:
            return None
        if len(periods) == 1:
            return periods[0].strftime("%b %Y")
        oldest, newest = periods[-1], periods[0]
        if oldest.year == newest.year:
            return f"{oldest.strftime('%b')} – {newest.strftime('%b %Y')}"
        return f"{oldest.strftime('%b %Y')} – {newest.strftime('%b %Y')}"

    cur_label   = "All time" if win == "all" else _period_label(incl_periods)
    prior_label = _period_label(cmp_periods) if cmp_periods else None

    def compute_window(district: str, incl: list, cmp: list) -> list:
        incl_set = set(incl)
        cmp_set  = set(cmp)
        if not incl_set:
            return []

        keys = {
            (cid, aid)
            for (cid, aid, dist, ch, per) in counts
            if dist == district and per in incl_set
        }

        firm_attorneys: dict = defaultdict(list)
        for cid, aid in keys:
            comp = comp_map.get(cid)
            atty = attorney_map.get(aid)
            if not comp or not atty:
                continue

            ch7_cur  = sum(counts.get((cid, aid, district, 7,  p), 0) for p in incl)
            ch13_cur = sum(counts.get((cid, aid, district, 13, p), 0) for p in incl)
            total_cur = ch7_cur + ch13_cur

            if cmp_set:
                ch7_pri  = sum(counts.get((cid, aid, district, 7,  p), 0) for p in cmp)
                ch13_pri = sum(counts.get((cid, aid, district, 13, p), 0) for p in cmp)
                total_pri = ch7_pri + ch13_pri
            else:
                ch7_pri = ch13_pri = total_pri = None

            atty_pct = (round((total_cur - total_pri) / total_pri * 100)
                        if (total_pri and total_pri > 0) else None)

            firm_attorneys[cid].append({
                "attorney":    atty.attorney_name,
                "ch7":         ch7_cur,
                "ch13":        ch13_cur,
                "total":       total_cur,
                "ch7_prior":   ch7_pri,
                "ch13_prior":  ch13_pri,
                "total_prior": total_pri,
                "pct_change":  atty_pct,
                "is_own_firm": comp.is_own_firm,
            })

        groups = []
        for cid, atty_rows in firm_attorneys.items():
            comp = comp_map.get(cid)
            if not comp:
                continue
            atty_rows.sort(key=lambda r: r["total"], reverse=True)

            firm_ch7   = sum(r["ch7"]  for r in atty_rows)
            firm_ch13  = sum(r["ch13"] for r in atty_rows)
            firm_total = firm_ch7 + firm_ch13
            firm_pri   = sum((r["total_prior"] or 0) for r in atty_rows) if cmp_set else None
            pct_change = (round((firm_total - firm_pri) / firm_pri * 100)
                          if (firm_pri and firm_pri > 0) else None)

            groups.append({
                "firm":        comp.name,
                "is_own_firm": comp.is_own_firm,
                "ch7":         firm_ch7,
                "ch13":        firm_ch13,
                "total":       firm_total,
                "total_prior": firm_pri,
                "pct_change":  pct_change,
                "attorneys":   atty_rows,
            })

        groups.sort(key=lambda g: (not g["is_own_firm"], -g["total"]))
        return groups

    def _district_total(district: str, periods: list) -> int | None:
        """Sum monthly district-wide case totals from discovery cache."""
        from app.models.discovery import DiscoveryCache
        total, found = 0, False
        for p in periods:
            key = f"{district.lower()}_district_total_{p.strftime('%Y_%m')}"
            row = db.query(DiscoveryCache).filter(DiscoveryCache.key == key).first()
            if row and row.value and row.value.get("total"):
                total += row.value["total"]
                found = True
        return total if found else None

    def _apply_market_share(groups: list, district_wide_total: int | None) -> list:
        denom = district_wide_total or sum(g["total"] for g in groups)
        for g in groups:
            g["market_share"] = round(g["total"] / denom * 100) if denom else 0
        return groups

    mdnc_rows = _apply_market_share(
        compute_window("MDNC", incl_periods, cmp_periods),
        _district_total("MDNC", incl_periods),
    )
    wdnc_rows = _apply_market_share(
        compute_window("WDNC", incl_periods, cmp_periods),
        _district_total("WDNC", incl_periods),
    )
    ednc_rows = _apply_market_share(
        compute_window("EDNC", incl_periods, cmp_periods),
        _district_total("EDNC", incl_periods),
    )

    mdnc_district_total = _district_total("MDNC", incl_periods)
    wdnc_district_total = _district_total("WDNC", incl_periods)
    ednc_district_total = _district_total("EDNC", incl_periods)

    # Aggregate totals per (competitor, district, period) for trend charts — uses all periods
    firm_period_totals: dict = defaultdict(int)
    for (cid, _aid, dist, _ch, per), count in counts.items():
        firm_period_totals[(cid, dist, per)] += count

    # Chapter-split totals for own-firm trend chart
    firm_chapter_period_totals: dict = defaultdict(int)
    for (cid, _aid, dist, ch, per), count in counts.items():
        firm_chapter_period_totals[(cid, dist, ch, per)] += count

    # District-level totals — total market volume per district per period
    district_period_totals: dict = defaultdict(int)
    for (cid, dist, per), val in firm_period_totals.items():
        district_period_totals[(dist, per)] += val

    def build_trend(district: str) -> dict:
        pairs = [(cid, per) for (cid, dist, per) in firm_period_totals if dist == district]
        if not pairs:
            return {"labels": [], "series": []}
        district_periods = sorted(set(per for (_, per) in pairs))
        if len(district_periods) < 2:
            return {"labels": [], "series": []}
        labels = [p.strftime("%b '%y") for p in district_periods]
        series = []
        for cid in set(cid for (cid, _) in pairs):
            comp = comp_map.get(cid)
            if not comp:
                continue
            data = [firm_period_totals.get((cid, district, per), 0) for per in district_periods]
            if any(d > 0 for d in data):
                name = comp.name if len(comp.name) <= 24 else comp.name[:23] + "…"
                series.append({"firm": name, "is_own": comp.is_own_firm, "is_total": False, "data": data})
        series.sort(key=lambda s: (not s["is_own"], -(s["data"][-1] if s["data"] else 0)))
        total_data = [district_period_totals.get((district, per), 0) for per in district_periods]
        if any(v > 0 for v in total_data):
            series.insert(0, {"firm": "Total Market", "is_own": False, "is_total": True, "data": total_data})
        return {"labels": labels, "series": series}

    mdnc_trend = build_trend("MDNC")
    wdnc_trend = build_trend("WDNC")
    ednc_trend = build_trend("EDNC")

    def build_mom_table(district: str) -> dict:
        all_dist_periods = sorted(
            {per for (cid, dist2, per) in firm_period_totals if dist2 == district},
            reverse=True,
        )
        if len(all_dist_periods) < 2:
            return {}
        display_periods = list(reversed(all_dist_periods[:6]))  # oldest → newest
        baseline_periods = all_dist_periods[1:13]               # up to 12 months prior
        period_totals = [
            district_period_totals.get((district, per), 0) for per in display_periods
        ]
        rows = []
        for cid in {cid for (cid, dist2, _) in firm_period_totals if dist2 == district}:
            comp = comp_map.get(cid)
            if not comp:
                continue
            data = [firm_period_totals.get((cid, district, per), 0) for per in display_periods]
            if not any(data):
                continue
            latest, prev = data[-1], data[-2]
            mom_abs = latest - prev
            mom_pct = round((latest - prev) / prev * 100) if prev else None
            shares = [
                round(count / total * 100) if total else 0
                for count, total in zip(data, period_totals)
            ]
            # Seasonal baseline: trailing 12-month average (excludes current period)
            if baseline_periods:
                b_vals = [firm_period_totals.get((cid, district, p), 0) for p in baseline_periods]
                avg_baseline = round(sum(b_vals) / len(b_vals)) if b_vals else None
            else:
                avg_baseline = None
            if avg_baseline:
                baseline_dev_abs = latest - avg_baseline
                baseline_dev_pct = round(baseline_dev_abs / avg_baseline * 100)
            else:
                baseline_dev_abs = baseline_dev_pct = None
            rows.append({
                "firm":             comp.name,
                "is_own":           comp.is_own_firm,
                "data":             data,
                "shares":           shares,
                "mom_abs":          mom_abs,
                "mom_pct":          mom_pct,
                "latest":           latest,
                "baseline_avg":     avg_baseline,
                "baseline_dev_abs": baseline_dev_abs,
                "baseline_dev_pct": baseline_dev_pct,
            })
        rows.sort(key=lambda r: (not r["is_own"], -(r["latest"] or 0)))

        # District-level baseline (market total, same methodology as per-firm)
        dist_b_vals = [district_period_totals.get((district, p), 0) for p in baseline_periods]
        dist_baseline_avg = round(sum(dist_b_vals) / len(dist_b_vals)) if dist_b_vals else None
        dist_latest = period_totals[-1]
        dist_prev = period_totals[-2] if len(period_totals) >= 2 else None
        dist_mom_abs = dist_latest - dist_prev if dist_prev is not None else None
        dist_mom_pct = round(dist_mom_abs / dist_prev * 100) if (dist_prev and dist_mom_abs is not None) else None
        if dist_baseline_avg:
            dist_dev_abs = dist_latest - dist_baseline_avg
            dist_dev_pct = round(dist_dev_abs / dist_baseline_avg * 100)
        else:
            dist_dev_abs = dist_dev_pct = None

        return {
            "labels":               [p.strftime("%b '%y") for p in display_periods],
            "rows":                 rows,
            "period_totals":        period_totals,
            "baseline_months":      len(baseline_periods),
            "dist_baseline_avg":    dist_baseline_avg,
            "dist_baseline_dev_pct": dist_dev_pct,
            "dist_baseline_dev_abs": dist_dev_abs,
            "dist_mom_abs":         dist_mom_abs,
            "dist_mom_pct":         dist_mom_pct,
        }

    mdnc_mom = build_mom_table("MDNC")
    wdnc_mom = build_mom_table("WDNC")
    ednc_mom = build_mom_table("EDNC")

    def build_chapter_trend(district: str) -> dict:
        """Ch7 vs Ch13 monthly split for own firm in a district."""
        own_cid = next((c.id for c in comp_map.values() if c.is_own_firm), None)
        if not own_cid:
            return {}
        periods = sorted({
            per for (cid, dist, ch, per) in firm_chapter_period_totals
            if cid == own_cid and dist == district
        })
        if len(periods) < 2:
            return {}
        return {
            "labels": [p.strftime("%b '%y") for p in periods],
            "ch7":    [firm_chapter_period_totals.get((own_cid, district, 7, p), 0) for p in periods],
            "ch13":   [firm_chapter_period_totals.get((own_cid, district, 13, p), 0) for p in periods],
        }

    mdnc_chapter = build_chapter_trend("MDNC")
    wdnc_chapter = build_chapter_trend("WDNC")

    def build_district_volume(district: str) -> dict:
        periods = sorted(
            {per for (dist, per) in district_period_totals if dist == district},
            reverse=True,
        )
        if not periods:
            return {}
        current_total = district_period_totals.get((district, periods[0]), 0)
        prior_total = district_period_totals.get((district, periods[1]), 0) if len(periods) > 1 else None
        mom_abs = (current_total - prior_total) if prior_total is not None else None
        mom_pct = round((current_total - prior_total) / prior_total * 100) if prior_total else None
        spark_periods = list(reversed(periods[:6]))
        # 12-month seasonal baseline — excludes current period
        hist_periods = periods[1:13]
        avg_12m = None
        dev_abs = None
        dev_pct = None
        if hist_periods:
            hist_vals = [district_period_totals.get((district, p), 0) for p in hist_periods]
            avg_12m = round(sum(hist_vals) / len(hist_vals))
            dev_abs = current_total - avg_12m
            dev_pct = round(dev_abs / avg_12m * 100) if avg_12m else None
        return {
            "current_period":   periods[0].strftime("%b %Y"),
            "current_total":    current_total,
            "prior_total":      prior_total,
            "mom_abs":          mom_abs,
            "mom_pct":          mom_pct,
            "sparkline":        [district_period_totals.get((district, p), 0) for p in spark_periods],
            "sparkline_labels": [p.strftime("%b '%y") for p in spark_periods],
            "avg_12m":          avg_12m,
            "dev_abs":          dev_abs,
            "dev_pct":          dev_pct,
            "hist_periods":     len(hist_periods),
        }

    mdnc_vol = build_district_volume("MDNC")
    wdnc_vol = build_district_volume("WDNC")
    ednc_vol = build_district_volume("EDNC")

    # Historical Duncan Law filing data (seeded from Excel)
    from app.models.discovery import DiscoveryCache
    _hist_row = db.query(DiscoveryCache).filter(
        DiscoveryCache.key == "duncan_law_filing_history"
    ).first()
    filing_history = _hist_row.value if _hist_row else None

    # Pre-compute year-over-year deltas and complete-year average in Python
    # (Jinja2 selectattr/rejectattr can't filter by truthy dict values reliably)
    historical_yoy_deltas: dict = {}
    historical_avg: int | None = None
    if filing_history and filing_history.get("annual"):
        _ann = filing_history["annual"]
        _prev_total: int | None = None
        _complete_totals: list = []
        for _row in _ann:  # already oldest→newest in seed data
            _is_complete = not _row.get("ytd") and not _row.get("partial")
            if _is_complete:
                if _prev_total is not None:
                    historical_yoy_deltas[_row["year"]] = _row["total"] - _prev_total
                _prev_total = _row["total"]
                _complete_totals.append(_row["total"])
        if _complete_totals:
            historical_avg = round(sum(_complete_totals) / len(_complete_totals))

    mdnc_discovery = get_cached_results(db, "MDNC")
    wdnc_discovery = get_cached_results(db, "WDNC")
    ednc_discovery = get_cached_results(db, "EDNC")

    from app.services.pacer import MARKET_TO_DISTRICT
    tracked_last_names: dict = {"MDNC": set(), "WDNC": set(), "EDNC": set()}
    for comp in comp_map.values():
        for loc in comp.locations:
            d = MARKET_TO_DISTRICT.get(loc.market)
            if d in tracked_last_names:
                for atty in comp.attorneys:
                    ln = _last_name(atty.attorney_name)
                    if ln:
                        tracked_last_names[d].add(ln)

    # Surface high-volume untracked attorneys from discovery data
    _SURFACE_THRESHOLD = 5  # cases/month minimum to flag

    def _surface_untracked(discovery, tracked_set):
        if not discovery or not discovery.get("top_filers"):
            return []
        out = []
        for f in discovery["top_filers"]:
            name = (f.get("attorney") or "").strip()
            if not name:
                continue
            last = _last_name(name)
            if last not in tracked_set and f.get("cases", 0) >= _SURFACE_THRESHOLD:
                out.append({"attorney": name, "cases": f["cases"]})
        return out  # already sorted by cases desc

    surfaced_mdnc = _surface_untracked(mdnc_discovery, tracked_last_names["MDNC"])
    surfaced_wdnc = _surface_untracked(wdnc_discovery, tracked_last_names["WDNC"])
    surfaced_ednc = _surface_untracked(ednc_discovery, tracked_last_names["EDNC"])

    return templates.TemplateResponse("filings.html", {
        "request":        request,
        "user":           user,
        "active_page":    "filings",
        "has_data":       True,
        "win":            win,
        "cur_label":      cur_label,
        "prior_label":    prior_label,
        "mdnc_rows":            mdnc_rows,
        "wdnc_rows":            wdnc_rows,
        "ednc_rows":            ednc_rows,
        "mdnc_district_total":  mdnc_district_total,
        "wdnc_district_total":  wdnc_district_total,
        "ednc_district_total":  ednc_district_total,
        "mdnc_discovery": mdnc_discovery,
        "wdnc_discovery": wdnc_discovery,
        "ednc_discovery": ednc_discovery,
        "mdnc_tracked":   tracked_last_names["MDNC"],
        "wdnc_tracked":   tracked_last_names["WDNC"],
        "ednc_tracked":   tracked_last_names["EDNC"],
        "mdnc_trend":     mdnc_trend,
        "wdnc_trend":     wdnc_trend,
        "ednc_trend":     ednc_trend,
        "mdnc_chapter":   mdnc_chapter,
        "wdnc_chapter":   wdnc_chapter,
        "mdnc_mom":       mdnc_mom,
        "wdnc_mom":       wdnc_mom,
        "ednc_mom":       ednc_mom,
        "mdnc_vol":       mdnc_vol,
        "wdnc_vol":       wdnc_vol,
        "ednc_vol":       ednc_vol,
        "surfaced_mdnc":   surfaced_mdnc,
        "surfaced_wdnc":   surfaced_wdnc,
        "surfaced_ednc":   surfaced_ednc,
        "filing_history":           filing_history,
        "historical_yoy_deltas":    historical_yoy_deltas,
        "historical_avg":           historical_avg,
    })
