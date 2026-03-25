#!/usr/bin/env bash
set -euo pipefail

RUNBOOK="docs/v2-cutover-runbook.md"
OPS_README="ops/README.md"

require_pattern() {
  local file="$1"
  local pattern="$2"
  local label="$3"

  if rg -qi "$pattern" "$file"; then
    echo "[ok] $label"
  else
    echo "[error] missing '$label' in $file"
    exit 1
  fi
}

for stage in 1 2 3 4 5; do
  require_pattern "$RUNBOOK" "Stage[[:space:]]+$stage" "runbook Stage $stage"
  require_pattern "$OPS_README" "Stage[[:space:]]+$stage" "ops README Stage $stage"
done

require_pattern "$RUNBOOK" "read traffic" "runbook read traffic semantics"
require_pattern "$RUNBOOK" "write traffic" "runbook write traffic semantics"
require_pattern "$RUNBOOK" "worker.*/.*event|worker[[:space:]]+event" "runbook worker/event semantics"
require_pattern "$RUNBOOK" "scheduler" "runbook scheduler semantics"
require_pattern "$RUNBOOK" "legacy observation|read-only observation" "runbook legacy observation semantics"

require_pattern "$OPS_README" "read traffic" "ops README read traffic semantics"
require_pattern "$OPS_README" "write traffic" "ops README write traffic semantics"
require_pattern "$OPS_README" "worker.*/.*event|worker[[:space:]]+event" "ops README worker/event semantics"
require_pattern "$OPS_README" "scheduler" "ops README scheduler semantics"
require_pattern "$OPS_README" "legacy observation|read-only observation" "ops README legacy observation semantics"
