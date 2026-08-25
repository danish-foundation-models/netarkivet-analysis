#!/usr/bin/env python3
"""Render the figures in analysis/figures/ from analysis/summary.json.

Usage: figures.py <summary.json> <domains_stats.tsv.gz> <outdir>
"""
import sys, json, gzip, math, itertools
from array import array
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

summary_path, stats_path, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
S = json.load(open(summary_path, encoding="utf-8"))

INK = "#1b1b1b"
ACCENT = "#2f6f9f"
MUTED = "#b8c9d6"
WARN = "#c2543d"

plt.rcParams.update({
    "figure.dpi": 130,
    "font.size": 9,
    "axes.edgecolor": "#888888",
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": INK,
    "ytick.color": INK,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})


def human(n, _=None):
    """1_250_000_000 -> "1.25B". Keeps enough precision that neighbouring axis
    ticks never collapse onto the same label (0f alone renders 1.25B, 1.5B and
    1.75B all as "1B" / "2B")."""
    n = float(n)
    for u, d in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= d:
            v = n / d
            s = f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{s}{u}"
    return f"{n:.0f}"


def pow2(k):
    """Label for log2 bucket k, in binary units. Bucket k holds lengths in
    [2**(k-1), 2**k - 1], so the tick names its lower edge: 11 -> "1k"."""
    k -= 1
    for u, e in (("G", 30), ("M", 20), ("k", 10)):
        if k >= e:
            return f"{1 << (k - e)}{u}"
    return str(1 << k)


def save(fig, name):
    fig.tight_layout()
    fig.savefig(f"{outdir}/{name}.svg", bbox_inches="tight")
    fig.savefig(f"{outdir}/{name}.png", bbox_inches="tight")
    plt.close(fig)
    print("wrote", name)


# ---- 1. records per year -------------------------------------------------
years = {int(k): v for k, v in S["by_year"].items()}
if years:
    ks = sorted(years)
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    ax.bar(ks, [years[k] for k in ks], color=ACCENT, width=0.75)
    ax.set_ylabel("records captured")
    ax.set_xlabel("capture year")
    ax.yaxis.set_major_formatter(FuncFormatter(human))
    ax.set_xticks([y for y in ks if y % 2 == 0])
    ax.set_xticklabels([str(y) for y in ks if y % 2 == 0], rotation=0)
    ax.set_title("Archive coverage over time", loc="left", fontsize=10, weight="bold")
    ax.grid(axis="y", color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)
    save(fig, "coverage_by_year")

# ---- 2. domain concentration --------------------------------------------
# The stats table has ~6.2M rows, so this holds the record counts in an
# array('q') rather than a list of Python ints, and plots a log-spaced sample
# of the curve instead of every point -- same picture, a fraction of the memory.
counts = array("q")
with gzip.open(stats_path, "rt", encoding="utf-8") as f:
    next(f)
    for line in f:
        counts.append(int(line.split("\t", 2)[1]))
counts = array("q", sorted(counts, reverse=True))
total = sum(counts)
if total:
    # Log-spaced ranks spanning the *whole* inventory, 1 .. len(counts). Sampling
    # only part of the range would draw the unsampled tail as a straight line.
    n = len(counts)
    steps = 800
    ranks = sorted({1, n} | {int(round(n ** (i / steps))) for i in range(steps + 1)})
    cum, run, at = [], 0, 0
    for r in ranks:
        run += sum(counts[at:r])
        at = r
        cum.append(run / total)
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.plot(ranks, cum, color=ACCENT, lw=1.8)
    ax.set_xscale("log")
    ax.set_xlabel("domains, ranked by record count (log scale)")
    ax.set_ylabel("cumulative share of records")
    ax.set_ylim(0, 1)
    ax.grid(color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)
    for n in (100, 10_000, 1_000_000):
        if n <= len(counts):
            share = sum(counts[:n]) / total
            ax.plot([n], [share], "o", color=WARN, ms=4)
            ax.annotate(f"top {human(n)}: {share*100:.0f}%",
                        (n, share), textcoords="offset points",
                        xytext=(6, -10), fontsize=8, color=WARN)
    half = next(i for i, v in enumerate(itertools.accumulate(counts), 1)
                if v * 2 >= total)
    ax.set_title(f"Half the archive is {half:,} of its {len(counts):,} domains",
                 loc="left",
                 fontsize=10, weight="bold")
    save(fig, "domain_concentration")

# ---- 3. text length distribution ----------------------------------------
lh = {int(k): v for k, v in S["text_length_hist_log2"].items()}
if lh:
    ks = [k for k in sorted(lh) if k <= 24]
    fig, ax = plt.subplots(figsize=(7.0, 3.0))
    ax.bar(ks, [lh[k] for k in ks], color=ACCENT, width=0.8)
    ax.set_xlabel("extracted text length (bytes, log₂ bucket lower edge)")
    ax.set_ylabel("records")
    ax.yaxis.set_major_formatter(FuncFormatter(human))
    ax.set_xticks([k for k in ks if k % 2 == 0])
    ax.set_xticklabels([pow2(k) if k else "0" for k in ks if k % 2 == 0])
    ax.grid(axis="y", color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Most captures carry very little text", loc="left",
                 fontsize=10, weight="bold")
    save(fig, "text_length_distribution")

# ---- 4. language mix -----------------------------------------------------
langs = S["languages"][:12]
if langs:
    fig, ax = plt.subplots(figsize=(6.0, 3.2))
    names = [d["lang"] for d in langs][::-1]
    vals = [d["records"] for d in langs][::-1]
    cols = [ACCENT if n == "da" else MUTED for n in names]
    ax.barh(range(len(names)), vals, color=cols)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names)
    ax.xaxis.set_major_formatter(FuncFormatter(human))
    ax.set_xlabel("records")
    ax.grid(axis="x", color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Detected content language", loc="left", fontsize=10, weight="bold")
    save(fig, "languages")

# ---- 5. biggest domains by extracted text -------------------------------
top = S["top_domains_by_text_bytes"][:20]
if top:
    fig, ax = plt.subplots(figsize=(6.6, 4.4))
    names = [d["domain"] for d in top][::-1]
    vals = [d["text_bytes"] for d in top][::-1]
    ax.barh(range(len(names)), vals, color=ACCENT)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels(names, fontsize=8)
    ax.xaxis.set_major_formatter(FuncFormatter(human))
    ax.set_xlabel("extracted text (bytes)")
    ax.grid(axis="x", color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)
    ax.set_title("Largest domains by extracted text", loc="left",
                 fontsize=10, weight="bold")
    save(fig, "top_domains_by_text")
