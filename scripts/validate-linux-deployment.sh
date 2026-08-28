#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
errors=0

check_file() {
    if [[ -f "$1" ]]; then
        printf 'PASS  %s\n' "$1"
    else
        printf 'FAIL  missing %s\n' "$1" >&2
        errors=$((errors + 1))
    fi
}

check_file "$ROOT_DIR/Dockerfile"
check_file "$ROOT_DIR/docker-compose.yml"
check_file "$ROOT_DIR/requirements-docker.txt"
check_file "$ROOT_DIR/scripts/start-linux.sh"
check_file "$ROOT_DIR/deploy/linux/securenexus.service"
check_file "$ROOT_DIR/deploy/linux/nginx-securenexus.conf"

if grep -q 'APP_HOST="\${APP_HOST:-0.0.0.0}"' "$ROOT_DIR/scripts/start-linux.sh"; then
    printf 'PASS  configurable Linux bind address\n'
else
    printf 'FAIL  Linux bind address is not configurable\n' >&2
    errors=$((errors + 1))
fi

if grep -q 'ALLOW_LEGACY_LOCAL_WORKSPACE=0' "$ROOT_DIR/deploy/linux/securenexus.service"; then
    printf 'PASS  production legacy workspace protection\n'
else
    printf 'FAIL  production legacy workspace protection missing\n' >&2
    errors=$((errors + 1))
fi

if [[ "$errors" -gt 0 ]]; then
    exit 1
fi
printf 'Linux deployment configuration is complete.\n'
