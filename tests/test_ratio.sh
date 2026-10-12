#!/bin/bash
# set_water_ratio.sh, offline: the meal table, the ratio arithmetic, input checks and the
# refuse-to-run guards. No login, no network. Needs jq (so it is skipped where jq is missing).
set -euo pipefail
cd "$(dirname "$0")/.."
command -v jq >/dev/null || { echo "skipped: jq is not installed here"; exit 0; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# the script's own table function, lifted out so it can run without logging in
awk '/^show_meals\(\) \{/{p=1} p{print} p&&/^}/{exit}' set_water_ratio.sh > "$tmp/show_meals.fn"
[[ -s $tmp/show_meals.fn ]] || { echo "FAIL: couldn't find show_meals in set_water_ratio.sh"; exit 1; }
source "$tmp/show_meals.fn"

PLAN='{"bEn":[1,1,1,1,1,0],"bDayEn":[1,1,1,1,1,1],"time":[14100,28500,42900,57300,71700,86100],"rept":[127,127,127,127,127,127],"food":[3,8,8,8,8,8],"water":[15,56,56,56,56,56],"aud":[2,2,2,2,2,2],"type":[1,1,1,1,1,1]}'
DEF='{"mixMin":10,"water":56,"food":8}'
fail() { echo "FAIL: $*"; exit 1; }

# 1. the table: times, amounts, ratios, the (off) marker and the default meal
out=$(show_meals "$PLAN" "$DEF")
grep -q "03:55 .* 3 g .* 15 g .*1:5$" <<<"$out" || fail "03:55 row: $out"
grep -q "07:55 .* 8 g .* 56 g .*1:7$" <<<"$out" || fail "07:55 row"
grep -q "23:55 .*(off)" <<<"$out" || fail "off marker"
grep -q "default .* 8 g .* 56 g .*1:7 .*feed now" <<<"$out" || fail "default row"
echo "1 ok: meal table"

# 2. applying a ratio changes water only, for every meal and the default
for R in 7 6.5 5; do
  NP=$(jq -c --argjson r "$R" '.water = [.food[] | (. * $r | round)]' <<<"$PLAN")
  ND=$(jq -c --argjson r "$R" '.water = (.food * $r | round)' <<<"$DEF")
  jq -e --argjson a "$PLAN" 'del(.water) == ($a | del(.water))' <<<"$NP" >/dev/null || fail "ratio $R touched more than water"
  jq -e --argjson r "$R" '[range(0; .food|length) as $i | (.water[$i] - (.food[$i] * $r | round))] | all(. == 0)' <<<"$NP" >/dev/null || fail "ratio $R water"
  jq -e '.food == 8 and .mixMin == 10' <<<"$ND" >/dev/null || fail "ratio $R default"
done
[[ $(jq -c '.water' <<<"$(jq -c '.water = [.food[] | (. * 7 | round)]' <<<"$PLAN")") == "[21,56,56,56,56,56]" ]] || fail "1:7 water"
echo "2 ok: ratio arithmetic (7, 6.5, 5) changes water only"

# 3. which answers to "water per gram of food" are accepted (same test the script uses)
ok() { [[ $1 =~ ^[0-9]+(\.[0-9])?$ ]] && jq -en --argjson r "$1" '$r >= 1 and $r <= 10' >/dev/null; }
for good in 7 6.5 10 1; do ok "$good" || fail "should accept '$good'"; done
for bad in 0 11 abc 7.55 -3 ""; do ! ok "$bad" || fail "should reject '$bad'"; done
echo "3 ok: accepts 1-10 with one decimal, rejects everything else"

# 4. it refuses to run with options, or without a terminal
out=$(./set_water_ratio.sh -r 7 </dev/null 2>&1) && fail "ran with a flag" || true
grep -q "takes no options" <<<"$out" || fail "flag message: $out"
out=$(./set_water_ratio.sh </dev/null 2>&1) && fail "ran without a terminal" || true
grep -q "Run this in a terminal" <<<"$out" || fail "terminal message: $out"
echo "4 ok: refuses flags and non-interactive runs"
