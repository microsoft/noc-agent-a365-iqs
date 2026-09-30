#!/usr/bin/env bash
# Deploy the NOC agent: default mode, or network-isolated with --network yes.
#
#   scripts/deploy.sh                     # current default deployment (NETWORK_ISOLATION=false)
#   scripts/deploy.sh --network yes       # private endpoints + VNet injection + jumpbox
#
# Anything after -- is passed to `azd up`. See docs/PRIVATE_NETWORKING.md.
set -euo pipefail

network=no
while [[ $# -gt 0 ]]; do
  case "$1" in
    --network) network="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$network" == yes || "$network" == no ]] || { echo "--network must be yes or no" >&2; exit 2; }

azd env set NETWORK_ISOLATION "$([[ "$network" == yes ]] && echo true || echo false)"
exec azd up "$@"
