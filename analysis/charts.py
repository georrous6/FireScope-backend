"""Charts for the harmonization step. Usage: python charts.py OUTDIR"""
import sys, json
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap, LogNorm

OUT = sys.argv[1]
for f in font_manager.findSystemFonts():
    if "Inter" in f: font_manager.fontManager.addfont(f)
plt.rcParams.update({"font.family": "Inter", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.spines.left": False,
                     "axes.edgecolor": "#c3c2b7", "axes.labelcolor": "#52514e",
                     "xtick.color": "#52514e", "ytick.color": "#52514e",
                     "axes.grid": True, "grid.color": "#e8e7e3", "grid.linewidth": 0.8,
                     "axes.axisbelow": True, "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb",
                     "savefig.facecolor": "#fcfcfb"})
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#8a8984"
BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#a8a7a1"
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]

n = pd.read_csv(f"{OUT}/harmonized_national_month.csv")
n["year"] = n.month.str[:4].astype(int); n["moy"] = n.month.str[5:].astype(int)
rep = json.load(open(f"{OUT}/report.json"))

def title(ax, t, sub, y=1.13):
    ax.text(0, y, t, transform=ax.transAxes, fontsize=13, weight="semibold", color=INK, va="bottom")
    ax.text(0, y - 0.07 if y > 1.1 else y - 0.03, sub, transform=ax.transAxes, fontsize=9.5, color=INK2, va="bottom")

# ---- Chart 1: overlap check (does converted MODIS land on VIIRS?)
y = n[(n.year >= 2012) & (n.year <= 2025)].groupby("year")[
    ["viirs_detections", "oos_modis_converted_detections", "modis_raw_detections"]].sum()
fig, ax = plt.subplots(figsize=(10, 4.6))
x = np.arange(len(y)); w = 0.27
specs = [("viirs_detections", BLUE, "VIIRS S-NPP (reference)"),
         ("oos_modis_converted_detections", ORANGE, "MODIS converted (year held out)"),
         ("modis_raw_detections", GRAY, "MODIS raw")]
for i, (c, col, lab) in enumerate(specs):
    ax.bar(x + (i - 1) * w, y[c], w - 0.04, color=col, label=lab, zorder=2)
ax.set_ylim(0, 12500)
ax.axvspan(8.5, len(y) - 0.5, ymax=0.9, color="#f2f1ed", zorder=0)
ax.text(8.65, 11000, "Not used for training\n(orbit-drift years)", fontsize=8.5, color=INK2, va="top")
ax.set_xticks(x, y.index); ax.set_ylabel("Fire detections per year")
ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}"))
ax.legend(frameon=False, loc="upper left", fontsize=9, ncols=3, bbox_to_anchor=(0, 1.02))
ax.grid(axis="x", visible=False); ax.tick_params(length=0)
title(ax, "Converted MODIS lands on VIIRS in every overlap year",
      "Annual vegetation-fire detections, Greece. Each orange bar comes from a model that never saw that year.")
fig.subplots_adjust(top=0.82, left=0.08, right=0.98, bottom=0.08)
fig.savefig(f"{OUT}/1_overlap_check.png", dpi=180); plt.close(fig)

# ---- Chart 2: the harmonized annual series 2001-2025
a = n[(n.year >= 2001) & (n.year <= 2025)].groupby("year").agg(
    det=("detections", "sum"), raw=("modis_raw_detections", "sum"), src=("source", "last"))
# Annual uncertainty: range of out-of-sample annual ratios (pred/VIIRS) in 2012-2020
def band(kind):
    r = np.array(list(rep["validation"][f"{kind}/v_n/hybrid/2012-2020_LOYO"]["annual_ratio_pred_over_viirs"].values()))
    return 1 / r.max(), 1 / r.min()
fig, ax = plt.subplots(figsize=(10, 4.6))
pre = a.index < 2012
cols = np.where(pre, ORANGE, BLUE)
ax.bar(a.index, a.det, 0.72, color=cols, zorder=2)
for yr, row in a[pre].iterrows():
    lo, hi = band("terra" if yr <= 2002 else "full")
    ax.plot([yr, yr], [row.det * lo, row.det * hi], color=INK2, lw=1.2, zorder=3)
ax.plot(a.index, a.raw, color=GRAY, lw=2, marker="o", ms=4, zorder=4, label="MODIS raw (for comparison)")
ax.axvline(2011.5, color=INK2, lw=1, ls=(0, (3, 3)))
ymax = 24500; ax.set_ylim(0, ymax)
ax.text(2011.35, ymax * 0.98, "MODIS converted to VIIRS-equivalent  ", ha="right", va="top", fontsize=9, color=INK2)
ax.text(2011.65, ymax * 0.98, "  VIIRS S-NPP", ha="left", va="top", fontsize=9, color=INK2)
ax.annotate("Terra only", (2001.5, a.det[2001] * 1.12), ha="center", fontsize=8, color=MUTED)
ax.set_xticks(range(2001, 2026, 2)); ax.set_ylabel("Fire detections per year (VIIRS-equivalent)")
ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("{x:,.0f}"))
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
ax.legend(handles=[Patch(color=ORANGE, label="Harmonized: from MODIS"), Patch(color=BLUE, label="Harmonized: VIIRS"),
                   Line2D([], [], color=INK2, lw=1.2, label="Out-of-sample error range"),
                   Line2D([], [], color=GRAY, lw=2, marker="o", ms=4, label="MODIS raw")],
          frameon=False, fontsize=9, ncols=4, loc="upper left", bbox_to_anchor=(0, -0.08))
ax.grid(axis="x", visible=False); ax.tick_params(length=0)
title(ax, "One series, 2001-2025, no step at the sensor switch",
      "Annual vegetation-fire detections in Greece, all years in S-NPP VIIRS-equivalent units.")
fig.subplots_adjust(top=0.84, left=0.08, right=0.98, bottom=0.17)
fig.savefig(f"{OUT}/2_harmonized_series.png", dpi=180); plt.close(fig)

# ---- Chart 3: the calendar preview (year x month)
c = n[(n.year >= 2001) & (n.year <= 2025)].pivot(index="year", columns="moy", values="detections")
fig, ax = plt.subplots(figsize=(8.6, 9.2))
cmap = LinearSegmentedColormap.from_list("seq", SEQ)
im = ax.imshow(c.values.clip(10), cmap=cmap, norm=LogNorm(10, c.values.max()), aspect="auto")
ax.set_xticks(range(12), ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
ax.set_yticks(range(len(c)), c.index)
ax.grid(False); ax.tick_params(length=0)
for s in ("bottom",): ax.spines[s].set_visible(False)
ax.set_xticks(np.arange(-.5, 12, 1), minor=True); ax.set_yticks(np.arange(-.5, len(c), 1), minor=True)
ax.grid(which="minor", color="#fcfcfb", linewidth=2); ax.tick_params(which="minor", length=0)
sw = list(c.index).index(2012) - 0.5
ax.axhline(sw, color=INK, lw=1.2)
ax.text(11.6, sw - 0.15, " from MODIS", fontsize=8.5, color=INK2, va="bottom", ha="left", clip_on=False)
ax.text(11.6, sw + 0.15, " VIIRS", fontsize=8.5, color=INK2, va="top", ha="left", clip_on=False)
cb = fig.colorbar(im, ax=ax, orientation="horizontal", fraction=0.03, pad=0.06, aspect=40)
cb.set_label("Detections per month (VIIRS-eq, log scale)", color=INK2); cb.outline.set_visible(False)
cb.ax.tick_params(colors=INK2, length=0)
title(ax, "Burning calendar preview: Greece, 2001-2025",
      "Summer wildfire season every year; autumn crop burning fades after 2013.", y=1.06)
fig.subplots_adjust(top=0.88, left=0.08, right=0.9, bottom=0.07)
fig.savefig(f"{OUT}/3_calendar_preview.png", dpi=180); plt.close(fig)
print("ok")
