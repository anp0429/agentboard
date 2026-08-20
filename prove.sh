#!/bin/zsh
# prove.sh — edgeverdict prove with the standing environment baked in.
#
# One afternoon (Aug 20) was lost to bare invocations after a terminal
# reset: no network consent (140s DNS backoff per install attempt), and
# default 2g/512m sandbox limits (OOM-killed monorepo installs at 90-165s
# a run) — every failure looking like tool instability. This wrapper makes
# the bare command impossible to get wrong. Override any var by exporting
# it before calling; flags pass straight through to prove.
#
# Usage:  ./prove.sh --intent "refactor(studio): derive notebook diff entries"

export EDGEVERDICT_WARM_CACHE="${EDGEVERDICT_WARM_CACHE:-1}"
export EDGEVERDICT_SANDBOX_NETWORK="${EDGEVERDICT_SANDBOX_NETWORK:-install}"
export EDGEVERDICT_SANDBOX_MEMORY="${EDGEVERDICT_SANDBOX_MEMORY:-8g}"
export EDGEVERDICT_TMPFS_SIZE="${EDGEVERDICT_TMPFS_SIZE:-8g}"
export EDGEVERDICT_SANDBOX_CPUS="${EDGEVERDICT_SANDBOX_CPUS:-4}"
export EDGEVERDICT_AUTO_SERVICES="${EDGEVERDICT_AUTO_SERVICES:-1}"

echo "prove.sh env: network=$EDGEVERDICT_SANDBOX_NETWORK memory=$EDGEVERDICT_SANDBOX_MEMORY tmpfs=$EDGEVERDICT_TMPFS_SIZE cpus=$EDGEVERDICT_SANDBOX_CPUS warm-cache=$EDGEVERDICT_WARM_CACHE"

exec edgeverdict prove "$@"
