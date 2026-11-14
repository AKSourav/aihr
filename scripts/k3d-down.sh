#!/usr/bin/env bash
set -euo pipefail

CLUSTER="${CLUSTER:-aihr}"

if k3d cluster list 2>/dev/null | grep -q "^$CLUSTER "; then
  k3d cluster delete "$CLUSTER"
  echo "cluster '$CLUSTER' deleted"
else
  echo "cluster '$CLUSTER' does not exist"
fi
