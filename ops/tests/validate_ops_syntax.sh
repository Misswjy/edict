#!/usr/bin/env bash
set -euo pipefail

SCRIPTS=(
  "ops/backup/backup_all.sh"
  "ops/restore/restore_all.sh"
  "ops/cutover/cutover_to_v2.sh"
  "ops/cutover/rollback_to_legacy.sh"
  "ops/cutover/reinject_tasks_placeholder.sh"
  "ops/tests/validate_reinject_tools.sh"
)

for script in "${SCRIPTS[@]}"; do
  bash -n "$script"
  echo "[ok] bash -n $script"
done

python3 -m py_compile \
  "ops/cutover/export_v2_delta.py" \
  "ops/cutover/merge_delta_into_legacy.py"
echo "[ok] py_compile ops/cutover/export_v2_delta.py"
echo "[ok] py_compile ops/cutover/merge_delta_into_legacy.py"
