#!/bin/sh
# Reproduce the analysis end to end.
#
#   src/run.sh /work/metadata            # source dir holding filtered_shard_*.csv
#
# Stage 1 scans every shard in parallel and writes per-shard aggregates to _agg/.
# Stage 2 merges them into data/ and analysis/. Stage 1 dominates: ~3.9 TiB at
# roughly 1.3 GB/s aggregate, so budget about two hours on a fast filesystem.
set -eu

SRC="${1:-/work/metadata}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGG="$ROOT/_agg"
JOBS="${JOBS:-24}"

mkdir -p "$AGG" "$ROOT/data" "$ROOT/analysis/figures"

echo "==> building scanner"
cc -O2 -o "$ROOT/src/shardstat" "$ROOT/src/shardstat.c"

# Shards 1..170. filtered_shard_90b is deliberately excluded: it is a byte-exact
# prefix of filtered_shard_90 (an aborted earlier export run) and including it
# would double-count 17.7 GB of records.
echo "==> scanning shards (${JOBS} at a time)"
seq 1 170 > "$AGG/shards.txt"
xargs -a "$AGG/shards.txt" -P "$JOBS" -n 1 sh -c '
  n="$0"
  [ -f "'"$AGG"'/done.$n" ] && exit 0
  "'"$ROOT"'/src/shardstat" "'"$SRC"'/filtered_shard_${n}-no-content.csv" "'"$AGG"'" "$n" \
    && touch "'"$AGG"'/done.$n" \
    || echo "FAILED $n" >> "'"$AGG"'/failures.log"
'

if [ -f "$AGG/failures.log" ]; then
  echo "!! some shards failed:" >&2
  cat "$AGG/failures.log" >&2
  exit 1
fi

echo "==> merging"
python3 "$ROOT/src/merge.py" "$AGG" "$ROOT/data" "$AGG/shards.txt"
mv "$ROOT/data/summary.json" "$ROOT/data/per_shard.tsv" "$ROOT/analysis/"

echo "==> figures"
python3 "$ROOT/src/figures.py" \
  "$ROOT/analysis/summary.json" \
  "$ROOT/data/domains_stats.tsv.gz" \
  "$ROOT/analysis/figures"

# The markdown tables in README.md are this file's output; regenerating it after a
# rescan shows exactly which published numbers moved.
echo "==> report"
python3 "$ROOT/src/report.py" "$ROOT/analysis/summary.json" > "$ROOT/analysis/report.md"

echo "==> done"
