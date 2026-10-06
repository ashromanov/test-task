#!/usr/bin/env bash
# Root-owned forced SSH command: accepts only "deploy <40-character commit SHA>".
set -euo pipefail
if [[ ! ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
    echo "Expected: deploy <commit SHA>" >&2
    exit 1
fi
revision=${BASH_REMATCH[1]}
base=/opt/async-payments
exec 9>"$base/deploy.lock"
flock 9
release="$base/releases/$revision"
mkdir -p "$release"
export RELEASE_DIR="$release"
# Python's data filter rejects path traversal, external symlinks and device files.
python3 -c 'import os,sys,tarfile; tarfile.open(fileobj=sys.stdin.buffer,mode="r|gz").extractall(os.environ["RELEASE_DIR"],filter="data")'
previous=$(readlink -f "$base/current" || true)
compose() {
    APP_IMAGE="async-payments:$revision" DEPLOY_REVISION="$revision" \
        docker compose --env-file "$base/.env" -p async-payments \
        -f "$release/compose.yaml" -f "$release/compose.prod.yaml" "$@"
}
compose build
if compose up -d --wait --wait-timeout 120; then
    # A running container must have a registered RabbitMQ consumer, not merely an alive PID.
    ready=false
    for _ in {1..30}; do
        if compose exec -T rabbitmq rabbitmqctl -q list_consumers | grep -q '^payments.new'; then
            ready=true
            break
        fi
        sleep 1
    done
    if $ready; then
        ln -sfn "$release" "$base/current"
        echo "DEPLOYED $revision"
        exit 0
    fi
fi
compose logs --tail 50 api consumer >&2
if [[ -n $previous && -d $previous ]]; then
    old_revision=$(basename "$previous")
    APP_IMAGE="async-payments:$old_revision" DEPLOY_REVISION="$old_revision" \
        docker compose --env-file "$base/.env" -p async-payments \
        -f "$previous/compose.yaml" -f "$previous/compose.prod.yaml" \
        up -d --no-build --wait --wait-timeout 120
    echo "ROLLED BACK application to $old_revision; database migrations are not reverted" >&2
fi
exit 1
