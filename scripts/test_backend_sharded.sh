#!/usr/bin/env bash
# Run the whole backend suite as N parallel file shards (a serial run is too slow).
# Usage: scripts/test_backend_sharded.sh [N=8] [extra pytest args]
# Needs backend/.venv (or JARVISOS_PYTHON); prints the failing test ids and exits 1 if any shard failed.
set -u
shards=${1:-8}
[ $# -gt 0 ] && shift
cd "$(dirname "$0")/../backend" || exit 2
python=${JARVISOS_PYTHON:-.venv/bin/python}
export LANG=C.UTF-8 LC_ALL=C.UTF-8
out=$(mktemp -d)
mapfile -t files < <(find tests -name 'test_*.py' | sort)
pids=()
for ((i = 0; i < shards; i++)); do
  shard=()
  for ((j = i; j < ${#files[@]}; j += shards)); do shard+=("${files[j]}"); done
  "$python" -m pytest -q -p no:cacheprovider -rfE "$@" "${shard[@]}" >"$out/shard-$i.log" 2>&1 &
  pids+=($!)
done
status=0
for ((i = 0; i < shards; i++)); do
  wait "${pids[i]}" || status=1
  tail -n 1 "$out/shard-$i.log"
  grep -E '^(FAILED|ERROR) ' "$out/shard-$i.log"
done
echo "logs: $out"
exit $status
