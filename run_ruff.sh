#!/bin/bash
echo "=== RUFF VERSION ==="
ruff --version 2>&1
echo "=== RUFF CHECK ==="
cd /Users/syntheticgio/Programming/local-bio-pipeliner
ruff check backend/app 2>&1
