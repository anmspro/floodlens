#!/usr/bin/env bash
# Full pipeline for every area. Usage: scripts/run_all_areas.sh [python]
set -u
PY=${1:-python}
AREAS="west_houston kingwood richmond simonton bevil_oaks hill_country"
for a in $AREAS; do
  for step in fetch-data imagery prepare "chips --force"; do
    echo "=== $a: $step ($(date +%H:%M:%S))"
    $PY -m floodlens $step --area $a || echo "!!! $a $step failed"
  done
done
for a in $AREAS; do
  echo "=== $a: label ($(date +%H:%M:%S))"
  $PY -m floodlens label --area $a || echo "!!! $a label failed"
  $PY -m floodlens fetch-context --area $a || echo "!!! $a fetch-context failed"
  $PY -m floodlens analyze --area $a || echo "!!! $a analyze failed"
  $PY -m floodlens impact --area $a || echo "!!! $a impact failed"
  $PY -m floodlens relief --area $a || echo "!!! $a relief failed"
done
echo "=== done ($(date +%H:%M:%S))"
