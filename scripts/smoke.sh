#!/usr/bin/env bash
# Live end-to-end check against a running stack. NOT run in CI.
set -euo pipefail
BASE="${1:-http://localhost:8080}"

echo "== healthz =="
curl -sf "$BASE/healthz" | python3 -m json.tool

echo "== dashboard =="
curl -sf "$BASE/api/dashboard" | python3 -c '
import json, sys
panels = json.load(sys.stdin)["panels"]
eq = panels["equity"]["rows"]
print(f"equity rows: {len(eq)} / expected 10")
missing = [r["symbol"] for r in eq if r["last"] is None]
bonds = panels["bonds"]["rows"]
print(f"bond rows: {len(bonds)} / expected 2")
assert any(r["cb_pct"] is not None for r in bonds), "no CB rates in bonds matrix"
assert any(r["y3m_pct"] is not None for r in bonds), "no 3M yields in bonds matrix"
past, upcoming = panels["macro"]["past"], panels["macro"]["releases"]
news_items = panels["news"]["items"]
print(f"macro past: {len(past)} upcoming: {len(upcoming)}")
print(f"news items: {len(news_items)}")
if missing:
    print(f"MISSING QUOTES (fix the yahoo symbol in config.yaml): {missing}")
    sys.exit(1)
'
echo "== series =="
curl -sf "$BASE/api/series/us-cpi-yoy?range=5y" | python3 -c '
import json, sys
pts = json.load(sys.stdin)["points"]
assert len(pts) > 30, f"us-cpi-yoy too short: {len(pts)}"
print(f"us-cpi-yoy points: {len(pts)} ok")
'
echo "== defi =="
curl -sf "$BASE/api/dashboard" | python3 -c '
import json, sys
panels = json.load(sys.stdin)["panels"]
defi = panels["defi"]["rows"]
mid = panels["midnight"]["rows"]
markets = panels["morpho"]["rows"]
refs = panels["refs"]["rows"]
print(f"defi pools: {len(defi)}")
print(f"midnight markets: {len(mid)}")
print(f"morpho markets: {len(markets)}")
print(f"refs rows: {len(refs)}")
# 5 aave markets x2 + pendle x2 + funding = 13 when every source is live
if len(refs) < 13:
    print(f"WARNING: only {len(refs)} refs rows (a source may be down or carried forward)")
assert defi, "no zyfai pools — check defiapi.zyf.ai"
assert refs, "no rate refs — check aave/pendle/binance sources"
assert markets, "no morpho markets — check blue-api.morpho.org/graphql"
# midnight can be legitimately empty between maturities; warn, do not fail
if not mid:
    print("WARNING: no live midnight markets (valid post-maturity gap)")
'

echo "SMOKE OK"
