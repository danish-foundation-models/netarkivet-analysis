#!/usr/bin/env python3
"""Render the markdown fragments used in README.md from analysis/summary.json.

Keeping this separate means every figure quoted in the README is derived from
the machine-readable summary rather than retyped by hand.

Usage: report.py analysis/summary.json
"""
import sys, json

S = json.load(open(sys.argv[1], encoding="utf-8"))
T, P, R = S["totals"], S["parse"], S["removal_audit"]


def n(x):
    return f"{x:,}"


def si(x, unit="B"):
    x = float(x)
    for suf, d in (("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if x >= d:
            return f"{x/d:.2f} {suf}{unit}"
    return f"{x:.0f} {unit}"


years = sorted(int(y) for y in S["by_year"])
recs = T["records"]

print("### Headline numbers\n")
print("| | |")
print("|---|---|")
print(f"| Records (URL captures) | **{n(recs)}** |")
print(f"| Unique domains | **{n(T['unique_domains'])}** |")
print(f"| Extracted text | **{si(T['text_bytes'])}** |")
print(f"| Distinct content hashes (est.) | {n(T['distinct_content_hashes_est'])} "
      f"({100*(1-T['distinct_content_hashes_est']/recs):.1f}% of records are exact "
      f"content duplicates) |")
print(f"| Distinct URLs (est.) | {n(T['distinct_urls_est'])} "
      f"({recs/T['distinct_urls_est']:.2f} captures per URL) |")
print(f"| Capture period | {years[0]}–{years[-1]} |")
print(f"| Shards scanned | {S['shards_processed']} |")

print("\n### Parse outcome\n")
print("| outcome | records | share |")
print("|---|---:|---:|")
trunc = P["trunc_len"] + P["trunc_url"]
for label, v in (("complete", P["complete"]),
                 ("truncated before `content_text_length`", P["trunc_len"]),
                 ("truncated inside the URL", P["trunc_url"]),
                 ("unparseable (dropped)", P["leftfail"])):
    print(f"| {label} | {n(v)} | {100*v/recs:.3f}% |")
print(f"\nTruncated records still yield a domain, date, status and language; "
      f"only `content_type` / `content_text_length` are lost for "
      f"{100*trunc/recs:.2f}% of records. Records lost entirely: "
      f"**{n(P['leftfail'])}**. Counter overflows: **{n(P['overflow_events'])}**.")

print("\n### Largest domains by extracted text\n")
print("| # | domain | records | extracted text |")
print("|---:|---|---:|---:|")
for i, d in enumerate(S["top_domains_by_text_bytes"][:15], 1):
    print(f"| {i} | `{d['domain']}` | {n(d['records'])} | {si(d['text_bytes'])} |")

print("\n### Largest domains by record count\n")
print("| # | domain | records | extracted text |")
print("|---:|---|---:|---:|")
for i, d in enumerate(S["top_domains_by_records"][:15], 1):
    print(f"| {i} | `{d['domain']}` | {n(d['records'])} | {si(d['text_bytes'])} |")

print("\n### Concentration\n")
print("| top N domains | share of records | share of text |")
print("|---:|---:|---:|")
for k in ("10", "100", "1000", "10000", "100000"):
    a = S["concentration"]["by_records"][k]["share"]
    b = S["concentration"]["by_text_bytes"][k]["share"]
    print(f"| {int(k):,} | {100*a:.1f}% | {100*b:.1f}% |")

print("\n### Language\n")
print("| language | records | share |")
print("|---|---:|---:|")
for d in S["languages"][:10]:
    print(f"| {d['lang']} | {n(d['records'])} | {100*d['records']/recs:.2f}% |")

print("\n### Content type\n")
print("| MIME type | records | share |")
print("|---|---:|---:|")
for d in S["mime_types"][:10]:
    print(f"| `{d['mime']}` | {n(d['records'])} | {100*d['records']/recs:.2f}% |")

print("\n### HTTP status\n")
print("| status | records | share |")
print("|---:|---:|---:|")
for d in S["status_codes"][:8]:
    print(f"| {d['status']} | {n(d['records'])} | {100*d['records']/recs:.2f}% |")

print("\n### TLDs\n")
print("| TLD | domains | records |")
print("|---|---:|---:|")
for d in S["tld"][:10]:
    print(f"| .{d['tld']} | {n(d['domains'])} | {n(d['records'])} |")

print("\n### Removal audit\n")
print(f"- listed for removal: **{n(R['listed'])}**")
print(f"- correctly absent from the export: **{n(R['correctly_absent'])}**")
print(f"- **still present (filter leak): {n(R['still_present'])}**, "
      f"accounting for {n(R['residual_records'])} records "
      f"({100*R['residual_record_share']:.4f}% of the archive) and "
      f"{si(R['residual_text_bytes'])} of text")
if R["malformed_list_entries"]:
    print(f"- malformed entries in the list (no TLD): "
          f"{', '.join('`'+e+'`' for e in R['malformed_list_entries'])}")
if R["leaks"]:
    print("\n| domain | records | extracted text | first capture | last capture |")
    print("|---|---:|---:|---:|---:|")
    for d in R["leaks"]:
        print(f"| `{d['domain']}` | {n(d['records'])} | {si(d['text_bytes'])} "
              f"| {d['first_capture']} | {d['last_capture']} |")

print("\n### Records per year\n")
print("| year | records |")
print("|---:|---:|")
for y in years:
    print(f"| {y} | {n(S['by_year'][str(y)])} |")
