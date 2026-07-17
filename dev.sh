#!/usr/bin/env bash
# =============================================================================
# Dev helper for the AI Chat Platform docker stack.
# Run from anywhere:  ./dev.sh <command>   (or: bash dev.sh <command>)
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-help}" in
  # ---- daily ops ----
  up)           docker compose up -d ;;
  down)         docker compose down ;;                       # stop + remove containers (keeps data)
  restart)      docker compose restart ;;
  logs)         docker compose logs -f --tail=50 ;;
  ps)           docker compose ps ;;

  # ---- dependency changes (the important ones) ----
  rebuild-backend)
    # Use after editing backend/requirements.txt
    docker compose up -d --build backend
    ;;
  rebuild-frontend)
    # Use after editing frontend/package.json
    # -V renews the node_modules anonymous volume so new deps actually load.
    docker compose up -d --build -V frontend
    ;;
  rebuild)
    # Rebuild both images (deps change on both sides, or Dockerfile edits)
    docker compose up -d --build
    ;;

  # ---- nuke data volumes (DB / cache / vectors / uploads) ----
  reset)        docker compose down -v ;;

  *)
    cat <<'EOF'
Usage: ./dev.sh <command>

Daily:
  up                 start the whole stack (detached)
  down               stop + remove containers (keeps data volumes)
  restart            restart containers
  logs               tail logs (Ctrl-C to exit)
  ps                 show container status

After changing dependencies:
  rebuild-backend    after editing backend/requirements.txt
  rebuild-frontend   after editing frontend/package.json  (renews node_modules)
  rebuild            rebuild both images

Reset:
  reset              stop + remove containers AND data volumes
                     (wipes postgres / redis / qdrant / uploads — irreversible)
EOF
    ;;
esac
