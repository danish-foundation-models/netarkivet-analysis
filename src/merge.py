#!/usr/bin/env python3
"""Merge per-shard aggregates into the published tables + summary.json.

Every accumulator is a Python int (arbitrary precision) -- the record count is
~1.7e10 and total text bytes ~1e14, both far past 32-bit, and floats are never
used for counting.

Usage: merge.py <aggdir> <outdir> [shard-list-file]
"""
import sys, os, gzip, json, math, glob, collections

aggdir, outdir = sys.argv[1], sys.argv[2]
shards = None
if len(sys.argv) > 3:
    shards = [l.strip() for l in open(sys.argv[3]) if l.strip()]
else:
    shards = sorted(
        (os.path.basename(p)[4:-4] for p in glob.glob(os.path.join(aggdir, "dom.*.tsv"))),
        key=lambda s: int(s) if s.isdigit() else 1 << 30,
    )
os.makedirs(outdir, exist_ok=True)
sys.stderr.write(f"merging {len(shards)} shards\n")

# domain -> [records, text_bytes, http200, first_capture, last_capture]
dom = {}
stat = collections.Counter()
lang = collections.Counter()
mime = collections.Counter()
status = collections.Counter()
ym = collections.Counter()
lenhist = collections.Counter()
HLL_M = 1 << 14
hllh = bytearray(HLL_M)
hllu = bytearray(HLL_M)
per_shard = {}

for n in shards:
    dp = os.path.join(aggdir, f"dom.{n}.tsv")
    mp = os.path.join(aggdir, f"misc.{n}.tsv")
    if not (os.path.exists(dp) and os.path.exists(mp)):
        sys.stderr.write(f"  !! missing output for shard {n}\n")
        continue
    with open(dp, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 6:
                continue
            d = p[0]
            rec, byt, ok, lo, hi = (int(x) for x in p[1:])
            e = dom.get(d)
            if e is None:
                dom[d] = [rec, byt, ok, lo if lo else 0, hi]
            else:
                e[0] += rec
                e[1] += byt
                e[2] += ok
                if lo and (e[3] == 0 or lo < e[3]):
                    e[3] = lo
                if hi > e[4]:
                    e[4] = hi
    with open(mp, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 3:
                continue
            tag, k, v = p
            v = int(v)
            if tag == "STAT":
                stat[k] += v
                per_shard.setdefault(n, {})[k] = v
            elif tag == "LANG":
                lang[k] += v
            elif tag == "MIME":
                mime[k] += v
            elif tag == "STATUS":
                status[k] += v
            elif tag == "YM":
                ym[k] += v
            elif tag == "LENHIST":
                lenhist[int(k)] += v
            elif tag == "HLLH":
                i = int(k)
                if v > hllh[i]:
                    hllh[i] = v
            elif tag == "HLLU":
                i = int(k)
                if v > hllu[i]:
                    hllu[i] = v
    sys.stderr.write(f"  shard {n}: {len(dom):,} domains so far\n")


def hll_estimate(reg):
    """Standard HLL with small-range correction; p=14 -> ~0.8% relative error."""
    m = len(reg)
    zeros = reg.count(0)
    s = 0.0
    for r in reg:
        s += 2.0 ** -r
    alpha = 0.7213 / (1 + 1.079 / m)
    e = alpha * m * m / s
    if e <= 2.5 * m and zeros:
        e = m * math.log(m / zeros)
    return int(e)


# ---------------- published tables ----------------
names = sorted(dom)
with gzip.open(os.path.join(outdir, "domains.txt.gz"), "wt",
               encoding="utf-8", compresslevel=6) as f:
    for d in names:
        f.write(d + "\n")

with gzip.open(os.path.join(outdir, "domains_stats.tsv.gz"), "wt",
               encoding="utf-8", compresslevel=6) as f:
    f.write("domain\trecords\ttext_bytes\tfirst_capture\tlast_capture\n")
    for d in names:
        r, b, ok, lo, hi = dom[d]
        f.write(f"{d}\t{r}\t{b}\t{lo}\t{hi}\n")

# ---------------- top-1000 leaderboards ----------------
def write_top(path, key_idx, header_note):
    ranked = sorted(dom.items(), key=lambda kv: (-kv[1][key_idx], kv[0]))[:1000]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {header_note}\n")
        f.write("rank\tdomain\trecords\ttext_bytes\tfirst_capture\tlast_capture\n")
        for i, (d, v) in enumerate(ranked, 1):
            f.write(f"{i}\t{d}\t{v[0]}\t{v[1]}\t{v[3]}\t{v[4]}\n")


write_top(os.path.join(outdir, "top1000_by_text.tsv"), 1,
          "Top 1000 domains by extracted text (text_bytes), largest first")
write_top(os.path.join(outdir, "top1000_by_records.tsv"), 0,
          "Top 1000 domains by number of captures (records), largest first")

# ---------------- removal audit ----------------
# The shards are already filtered: domains on this list were dropped before the
# export reached us. So the useful question is not "how much do they weigh" but
# "did the filter actually catch all of them".
removed_path = os.environ.get(
    "REMOVED_LIST",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "data", "domains_removed.txt"))
removed = [l.strip().lower() for l in
           open(removed_path, encoding="utf-8") if l.strip()]
removed_set = set(removed)
present = removed_set & set(dom)
rem_rec = sum(dom[d][0] for d in present)
rem_byt = sum(dom[d][1] for d in present)
malformed_entries = sorted(d for d in removed_set if "." not in d)

total_rec = sum(v[0] for v in dom.values())
total_byt = sum(v[1] for v in dom.values())

# ---------------- concentration ----------------
by_rec = sorted(dom.items(), key=lambda kv: -kv[1][0])
by_byt = sorted(dom.items(), key=lambda kv: -kv[1][1])


def topshare(sorted_items, idx, total, ns=(10, 100, 1000, 10000, 100000)):
    out, run, k = {}, 0, 0
    for n in ns:
        while k < n and k < len(sorted_items):
            run += sorted_items[k][1][idx]
            k += 1
        out[str(n)] = {"count": run,
                       "share": round(run / total, 6) if total else 0.0}
    return out


tld = collections.Counter()
tld_rec = collections.Counter()
for d, v in dom.items():
    t = d.rsplit(".", 1)[-1] if "." in d else "(none)"
    tld[t] += 1
    tld_rec[t] += v[0]

summary = {
    "shards_processed": len([n for n in shards
                             if os.path.exists(os.path.join(aggdir, f"dom.{n}.tsv"))]),
    "parse": {k: stat[k] for k in
              ("records", "complete", "trunc_len", "trunc_url", "leftfail",
               "nodomain", "baddate", "badstatus", "overflow_events", "oversize")},
    "totals": {
        "records": total_rec,
        "unique_domains": len(dom),
        "text_bytes": total_byt,
        "distinct_content_hashes_est": hll_estimate(hllh),
        "distinct_urls_est": hll_estimate(hllu),
    },
    "removal_audit": {
        "listed": len(removed_set),
        "correctly_absent": len(removed_set - set(dom)),
        "still_present": len(present),
        "residual_records": rem_rec,
        "residual_record_share": round(rem_rec / total_rec, 9) if total_rec else 0.0,
        "residual_text_bytes": rem_byt,
        "malformed_list_entries": malformed_entries,
        "leaks": [
            {"domain": d, "records": dom[d][0], "text_bytes": dom[d][1],
             "first_capture": dom[d][3], "last_capture": dom[d][4]}
            for d in sorted(present, key=lambda x: -dom[x][0])
        ],
    },
    "concentration": {
        "by_records": topshare(by_rec, 0, total_rec),
        "by_text_bytes": topshare(by_byt, 1, total_byt),
    },
    "top_domains_by_records": [
        {"domain": d, "records": v[0], "text_bytes": v[1]} for d, v in by_rec[:50]],
    "top_domains_by_text_bytes": [
        {"domain": d, "records": v[0], "text_bytes": v[1]} for d, v in by_byt[:50]],
    "languages": [{"lang": k, "records": v} for k, v in lang.most_common(30)],
    "mime_types": [{"mime": k, "records": v} for k, v in mime.most_common(30)],
    "status_codes": [{"status": k, "records": v} for k, v in status.most_common(30)],
    "by_year": {},
    "by_year_month": {k: ym[k] for k in sorted(ym)},
    "text_length_hist_log2": {str(k): lenhist[k] for k in sorted(lenhist)},
    "tld": [{"tld": k, "domains": v, "records": tld_rec[k]}
            for k, v in tld.most_common(25)],
}
for k, v in ym.items():
    y = k[:4]
    summary["by_year"][y] = summary["by_year"].get(y, 0) + v
summary["by_year"] = {k: summary["by_year"][k] for k in sorted(summary["by_year"])}

with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)

# per-shard record counts, useful for spotting a bad shard
with open(os.path.join(outdir, "per_shard.tsv"), "w", encoding="utf-8") as f:
    f.write("shard\trecords\tcomplete\ttrunc_len\ttrunc_url\tleftfail\tuniq_domains\n")
    for n in shards:
        s = per_shard.get(n)
        if s:
            f.write(f"{n}\t{s.get('records',0)}\t{s.get('complete',0)}\t"
                    f"{s.get('trunc_len',0)}\t{s.get('trunc_url',0)}\t"
                    f"{s.get('leftfail',0)}\t{s.get('uniq_domains',0)}\n")

sys.stderr.write(
    f"\ndomains={len(dom):,} records={total_rec:,} text_bytes={total_byt:,}\n"
    f"removal audit: {len(present):,}/{len(removed_set):,} listed domains still "
    f"present ({rem_rec:,} residual records)\n")
