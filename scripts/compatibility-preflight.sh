#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
environment_file="$project_root/environment.yml"
provider_evidence="$project_root/.kiro/specs/foundation-and-schema/provider-compatibility.json"

if [[ ! -f "$environment_file" ]]; then
  echo "FAIL: missing source-of-truth environment.yml" >&2
  exit 1
fi

conda env list
if ! conda run -n mlsc python -V >/dev/null 2>&1; then
  conda env create -f "$environment_file"
fi

conda env list
conda run -n mlsc python "$project_root/scripts/verify_compatibility.py" \
  "$environment_file" "$provider_evidence"
