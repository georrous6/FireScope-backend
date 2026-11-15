"""
MODIS -> VIIRS hotspot harmonization (Greece, FIRMS archive files)
==================================================================
Usage:
    python harmonize.py MODIS.csv SNPP.csv NOAA20.csv NOAA21.csv OUTDIR

Produces a single, sensor-independent monthly series of fire activity on a
0.25 deg grid, expressed in "S-NPP VIIRS-equivalent" units:
    - 2000-11 .. 2012-01 : modelled from MODIS (converted to VIIRS-equivalent)
    - 2012-02 .. 2026-07 : VIIRS S-NPP (outage days filled with NOAA-20)

Steps (match the numbered sections below):
  1. Clean all sensors with the same rules
  2. Aggregate to grid cell x month
  3. Build one consistent VIIRS reference series
  4. Build MODIS features (Terra+Aqua, and Terra-only for 2000-2002)
  5. Fit MODIS -> VIIRS conversion, validate leaving one year out
  6. Uncertainty from out-of-sample errors
  7. Merge into the harmonized series + break checks
"""
import sys, os, json
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

MODIS_F, SNPP_F, N20_F, N21_F, OUT = sys.argv[1:6]
os.makedirs(OUT, exist_ok=True)
GRID = 0.25                       # degrees
MODIS_MIN_CONF = 30               # MODIS confidence is 0-100
AQUA_START = pd.Period("2002-07", "M")
SWITCH = pd.Period("2012-02", "M")  # first full VIIRS month (S-NPP starts 2012-01-20)
VIIRS_END = pd.Period("2026-07", "M")
report = {}

# ---------------------------------------------------------------- 1. CLEAN
def load(path, sensor):
    d = pd.read_csv(path)
    n0 = len(d)
    d = d[d["type"] == 0]                                   # vegetation fires only
    if sensor == "MODIS":
        d = d[d["confidence"] >= MODIS_MIN_CONF]
    else:
        d = d[d["confidence"] != "l"]                        # drop low-confidence VIIRS
    d = d.copy()
    d["date"] = pd.to_datetime(d["acq_date"])
    d["month"] = d["date"].dt.to_period("M")
    d["gx"] = np.floor(d["longitude"] / GRID).astype(int)
    d["gy"] = np.floor(d["latitude"] / GRID).astype(int)
    report.setdefault("cleaning", {})[path.split("/")[-1]] = {"raw": n0, "kept": len(d)}
    return d

modis = load(MODIS_F, "MODIS")
snpp, n20, n21 = load(SNPP_F, "V"), load(N20_F, "V"), load(N21_F, "V")

# ------------------------------------------- 3a. S-NPP outage days -> NOAA-20
# FIRMS lists detections only, so an outage looks like "no fire". Flag days
# where S-NPP has nothing but NOAA-20 sees >= 5 fires (e.g. 26 Jul - 10 Aug 2022).
# Single empty days are usually clouds/overpass geometry, so only runs of
# >= 3 consecutive empty S-NPP days during which NOAA-20 saw >= 15 fires count.
sd = snpp.groupby("date").size()
nd = n20.groupby("date").size()
days = pd.date_range(n20.date.min(), snpp.date.max())
empty = pd.Series([sd.get(d, 0) == 0 for d in days], index=days)
run_id = (empty != empty.shift()).cumsum()
outage = []
for _, run in empty[empty].groupby(run_id[empty]):
    if len(run) >= 3 and nd.reindex(run.index).fillna(0).sum() >= 15:
        outage += list(run.index)
# NOAA-20 -> S-NPP scale factors from the days both observed
both = sd.index.intersection(nd.index)
cnt_scale = sd[both].sum() / nd[both].sum()
frp_scale = (snpp[snpp.date.isin(both)].frp.sum() / n20[n20.date.isin(both)].frp.sum())
fill = n20[n20.date.isin(outage)].copy()
fill["w"] = cnt_scale
fill["frp"] = fill["frp"] * frp_scale
snpp["w"] = 1.0
viirs = pd.concat([snpp, fill], ignore_index=True)
report["snpp_outage_days_filled"] = [str(d.date()) for d in sorted(outage)]
report["noaa20_to_snpp"] = {"count": round(cnt_scale, 3), "frp": round(frp_scale, 3)}
# Cross-calibration of NOAA-21 too (for continuity after S-NPP ends)
n21d = n21.groupby("date").size(); b21 = sd.index.intersection(n21d.index)
report["noaa21_to_snpp"] = {
    "count": round(sd[b21].sum() / n21d[b21].sum(), 3),
    "frp": round(snpp[snpp.date.isin(b21)].frp.sum() / n21[n21.date.isin(b21)].frp.sum(), 3)}

# ------------------------------------------------------- 2. AGGREGATE
cells = pd.concat([modis[["gx", "gy"]], viirs[["gx", "gy"]]]).drop_duplicates()
months = pd.period_range(modis.month.min(), VIIRS_END, freq="M")
idx = pd.MultiIndex.from_product([months], names=["month"])
grid = (cells.assign(k=1).merge(pd.DataFrame({"month": months, "k": 1}), on="k")
        .drop(columns="k").set_index(["gx", "gy", "month"]).sort_index())

v_agg = viirs.groupby(["gx", "gy", "month"]).agg(v_n=("w", "sum"), v_frp=("frp", "sum"))
grid = grid.join(v_agg).fillna({"v_n": 0, "v_frp": 0})

# ----------------------------------------------- 4. MODIS FEATURES
def modis_feats(d, tag):
    g = d.groupby(["gx", "gy", "month"])
    f = pd.DataFrame({
        f"{tag}_n": g.size(),
        f"{tag}_frp": g.frp.sum(),
        f"{tag}_frpmax": g.frp.max(),
        f"{tag}_conf": g.confidence.mean(),
        f"{tag}_night": g.daynight.apply(lambda s: (s == "N").sum()),
    })
    return f

F_all = modis_feats(modis, "m")
F_terra = modis_feats(modis[modis.satellite == "Terra"], "t")
grid = grid.join(F_all).join(F_terra)
for c in grid.columns:
    if c.startswith(("m_", "t_")):
        grid[c] = grid[c].fillna(0 if not c.endswith("conf") else -1)
grid = grid.reset_index()
grid["moy"] = grid.month.dt.month
grid["lat"] = (grid.gy + 0.5) * GRID
grid["lon"] = (grid.gx + 0.5) * GRID

# Neighbourhood (3x3 cells) MODIS activity: catches fires just across a cell edge
def neigh(col):
    p = grid.pivot_table(index=["month"], columns=["gx", "gy"], values=col, fill_value=0)
    out = {}
    keys = set(p.columns)
    for (x, y) in p.columns:
        s = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (x + dx, y + dy) in keys:
                    s = s + p[(x + dx, y + dy)]
        out[(x, y)] = s
    q = pd.DataFrame(out).stack([0, 1], future_stack=True).rename(col + "_nb").reset_index()
    q.columns = ["month", "gx", "gy", col + "_nb"]
    return q
grid = grid.merge(neigh("m_n"), on=["month", "gx", "gy"]).merge(neigh("t_n"), on=["month", "gx", "gy"])

# Cell climatology from MODIS only (exists for every year, so it's a fair input)
for tag in ("m", "t"):
    clim = grid[grid.month >= AQUA_START].groupby(["gx", "gy", "moy"])[f"{tag}_n"].mean().rename(f"{tag}_clim")
    grid = grid.merge(clim.reset_index(), on=["gx", "gy", "moy"], how="left")

FEATS = {"full": ["m_n", "m_frp", "m_frpmax", "m_conf", "m_night", "m_n_nb", "m_clim", "moy", "lat", "lon"],
         "terra": ["t_n", "t_frp", "t_frpmax", "t_conf", "t_night", "t_n_nb", "t_clim", "moy", "lat", "lon"]}

class LogModel:
    """Gradient boosting on log(1+y). A Poisson/log-link version was tried first
    and exploded (x10^6) on extreme cells: tree effects multiply in log space.
    Squared error on log1p keeps predictions inside the training range; a
    single rescaling factor (fitted on training data) removes the back-transform bias.
    Monotonic constraints: more MODIS activity can never mean less VIIRS activity."""
    def __init__(self, feats):
        mono = [1 if f.endswith(("_n", "_frp", "_frpmax", "_night", "_nb", "_clim")) else 0 for f in feats]
        self.m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                               min_samples_leaf=40, monotonic_cst=mono, random_state=0)
    def fit(self, X, y):
        self.m.fit(X, np.log1p(y))
        self.k = y.sum() / np.expm1(self.m.predict(X)).clip(0).sum()
        return self
    def predict(self, X):
        return np.expm1(self.m.predict(X)).clip(0) * self.k

def model(kind):
    return LogModel(FEATS[kind])

# ------------------------------------- 5. FIT + LEAVE-ONE-YEAR-OUT VALIDATION
# Train only on the stable-orbit years (2012-2020). Terra and Aqua drift to
# different overpass times from ~2020-2022 on; pre-2012 MODIS looked like 2012-2020,
# so that's the period whose MODIS->VIIRS relation we transfer back in time.
# 2021-2026 is kept as an extra "drift check".
TRAIN_END = pd.Period("2020-12", "M")
grid["year"] = grid.month.dt.year
overlap = grid[(grid.month >= SWITCH) & (grid.month <= VIIRS_END)].copy()
in_train = overlap.month <= TRAIN_END
years = sorted(overlap[in_train].year.unique())

# Baseline: national ratio by month-of-year, applied to each cell's MODIS value
def ratio_fit(tr, src, target):
    s = tr.groupby("moy")[[src, target]].sum()
    return (s[target] / s[src].replace(0, np.nan)).fillna(s[target].sum() / s[src].sum())

def hybrid(df, model_col, base_col):
    """Spatial pattern from the model, national monthly total from the ratio baseline."""
    tot_m = df.groupby("month")[model_col].transform("sum")
    tot_b = df.groupby("month")[base_col].transform("sum")
    return np.where(tot_m > 0, df[model_col] * tot_b / tot_m.replace(0, np.nan), df[base_col])

def fit_predict(tr, te, kind, target):
    src = {"full": {"v_n": "m_n", "v_frp": "m_frp"}, "terra": {"v_n": "t_n", "v_frp": "t_frp"}}[kind][target]
    mdl = model(kind).fit(tr[FEATS[kind]], tr[target])
    out = te[["month"]].copy()
    out["model"] = mdl.predict(te[FEATS[kind]])
    out["ratio"] = te[src].values * te["moy"].map(ratio_fit(tr, src, target)).values
    out["hybrid"] = hybrid(out, "model", "ratio")
    return out

for kind in ("full", "terra"):
    for target in ("v_n", "v_frp"):
        # leave-one-year-out inside 2012-2020
        for y in years:
            tr = overlap[in_train & (overlap.year != y)]; te_m = in_train & (overlap.year == y)
            p = fit_predict(tr, overlap[te_m], kind, target)
            for c in ("model", "ratio", "hybrid"):
                overlap.loc[te_m, f"oos_{kind}_{target}_{c}"] = p[c].values
        # drift check: train on all 2012-2020, predict 2021-2026
        te_m = ~in_train
        p = fit_predict(overlap[in_train], overlap[te_m], kind, target)
        for c in ("model", "ratio", "hybrid"):
            overlap.loc[te_m, f"oos_{kind}_{target}_{c}"] = p[c].values
train = overlap

def scores(df, pred, target):
    nat = df.groupby("month")[[pred, target]].sum()
    yr = df.groupby("year")[[pred, target]].sum()
    lr = np.log((nat[pred] + 1) / (nat[target] + 1))
    return {
        "national_monthly_corr": round(nat[pred].corr(nat[target]), 3),
        "national_monthly_median_abs_err_pct": round(100 * np.median(np.abs(np.expm1(lr))), 1),
        "annual_ratio_pred_over_viirs": {int(k): round(v, 2) for k, v in (yr[pred] / yr[target]).items()},
        "cell_month_MAE": round(np.mean(np.abs(df[pred] - df[target])), 3),
        "share_of_viirs_fire_cells_predicted": round(((df[pred] > 0.5) & (df[target] > 0)).sum() / (df[target] > 0).sum(), 3),
        "share_of_predicted_fire_cells_correct": round(((df[pred] > 0.5) & (df[target] > 0)).sum() / max(1, (df[pred] > 0.5).sum()), 3),
    }

val = {}
for kind in ("full", "terra"):
    for target in ("v_n", "v_frp"):
        for c in ("ratio", "model", "hybrid"):
            col = f"oos_{kind}_{target}_{c}"
            val[f"{kind}/{target}/{c}/2012-2020_LOYO"] = scores(train[in_train], col, target)
            val[f"{kind}/{target}/{c}/2021-2026_drift"] = scores(train[~in_train], col, target)
report["validation"] = val
METHOD = "hybrid"

# --------------------------------------------- 6. UNCERTAINTY (national month)
# Empirical 10-90% band of out-of-sample error, by activity level
unc = {}
for kind in ("full", "terra"):
    for target in ("v_n", "v_frp"):
        nat = train[in_train].groupby("month")[[f"oos_{kind}_{target}_{METHOD}", target]].sum()
        p, o = nat.iloc[:, 0], nat.iloc[:, 1]
        lr = np.log((o + 1) / (p + 1))
        hi_act = p >= p.median()
        unc[(kind, target)] = {lvl: (float(lr[m].quantile(.1)), float(lr[m].quantile(.9)))
                               for lvl, m in (("low", ~hi_act), ("high", hi_act))}
        unc[(kind, target)]["split"] = float(p.median())
report["uncertainty_log_bands"] = {f"{k[0]}/{k[1]}": v for k, v in unc.items()}

# ------------------------------------------- 7. FINAL MODELS + MERGED SERIES
tr_final = train[in_train]
for kind in ("full", "terra"):
    for target in ("v_n", "v_frp"):
        p = fit_predict(tr_final, grid, kind, target)
        grid[f"pred_{kind}_{target}"] = p[METHOD].values

grid["kind"] = np.where(grid.month < AQUA_START, "terra", "full")
for t in ("v_n", "v_frp"):
    grid[f"modis_eq_{t}"] = np.where(grid.kind == "terra", grid[f"pred_terra_{t}"], grid[f"pred_full_{t}"])
    grid[f"harm_{t}"] = np.where(grid.month >= SWITCH, grid[t], grid[f"modis_eq_{t}"])
grid["source"] = np.where(grid.month >= SWITCH, "VIIRS S-NPP",
                  np.where(grid.kind == "terra", "MODIS Terra -> VIIRS-eq", "MODIS Terra+Aqua -> VIIRS-eq"))

cell_out = grid[["month", "gx", "gy", "lat", "lon", "source", "harm_v_n", "harm_v_frp",
                 "modis_eq_v_n", "modis_eq_v_frp", "v_n", "v_frp", "m_n", "m_frp"]].copy()
cell_out.columns = ["month", "gx", "gy", "lat", "lon", "source", "detections", "frp_mw",
                    "modis_converted_detections", "modis_converted_frp_mw",
                    "viirs_detections", "viirs_frp_mw", "modis_raw_detections", "modis_raw_frp_mw"]
cell_out["month"] = cell_out.month.astype(str)
num = cell_out.select_dtypes("number").columns.difference(["gx", "gy", "lat", "lon"])
cell_out[num] = cell_out[num].round(3)
cell_out = cell_out[(cell_out.select_dtypes("number").drop(columns=["gx", "gy", "lat", "lon"]) > 0.005).any(axis=1)]
cell_out.to_csv(f"{OUT}/harmonized_cell_month.csv", index=False)

nat = grid.groupby("month").agg(
    detections=("harm_v_n", "sum"), frp_mw=("harm_v_frp", "sum"),
    modis_converted_detections=("modis_eq_v_n", "sum"), modis_converted_frp_mw=("modis_eq_v_frp", "sum"),
    viirs_detections=("v_n", "sum"), viirs_frp_mw=("v_frp", "sum"),
    modis_raw_detections=("m_n", "sum"), modis_raw_frp_mw=("m_frp", "sum"), kind=("kind", "first")).reset_index()
nat["source"] = np.where(nat.month >= SWITCH, "VIIRS S-NPP",
                 np.where(nat.kind == "terra", "MODIS Terra -> VIIRS-eq", "MODIS Terra+Aqua -> VIIRS-eq"))
for t, col in (("v_n", "detections"), ("v_frp", "frp_mw")):
    lo, hi = [], []
    for _, r in nat.iterrows():
        if r.month >= SWITCH:
            lo.append(r[col]); hi.append(r[col]); continue
        u = unc[(r.kind, t)]; band = u["high"] if r[col] >= u["split"] else u["low"]
        lo.append(max(0, (r[col] + 1) * np.exp(band[0]) - 1)); hi.append((r[col] + 1) * np.exp(band[1]) - 1)
    nat[col + "_p10"], nat[col + "_p90"] = lo, hi
# out-of-sample MODIS prediction in overlap (for the break check chart)
oos_nat = train.groupby("month")[[f"oos_full_v_n_{METHOD}", f"oos_full_v_frp_{METHOD}", f"oos_terra_v_n_{METHOD}"]].sum()
oos_nat.columns = ["oos_modis_converted_detections", "oos_modis_converted_frp_mw", "oos_terra_only_converted_detections"]
nat = nat.merge(oos_nat.reset_index(), on="month", how="left")
nat = nat.drop(columns="kind")
nat["month"] = nat.month.astype(str)
nat.round(2).to_csv(f"{OUT}/harmonized_national_month.csv", index=False)

# Break check across the Terra-only -> Terra+Aqua switch (2002-07):
ov = grid[(grid.month >= AQUA_START) & (grid.month < SWITCH)]
report["aqua_switch_check_2002_2011"] = {
    "terra_only_model_total": round(ov.pred_terra_v_n.sum()), "terra_aqua_model_total": round(ov.pred_full_v_n.sum()),
    "ratio": round(ov.pred_terra_v_n.sum() / ov.pred_full_v_n.sum(), 3)}

with open(f"{OUT}/report.json", "w") as f:
    json.dump(report, f, indent=1, default=str)
print(json.dumps(report, indent=1, default=str))
