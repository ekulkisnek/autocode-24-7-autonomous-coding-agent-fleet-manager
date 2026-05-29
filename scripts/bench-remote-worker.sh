#!/usr/bin/env bash
# Benchmark Windows remote worker latency (SSH ping, mkdir, scp, smoke read).
set -euo pipefail

WORKER_ID="${1:-windows-main}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== autocode worker bench $WORKER_ID ==="
python3 -m autocode worker bench "$WORKER_ID"

echo
echo "=== autocode worker coord ==="
python3 -m autocode worker coord
