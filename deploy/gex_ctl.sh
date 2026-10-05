#!/bin/zsh
# Control script for the gex-service launchd agent (macOS host running IB Gateway).
#
#   deploy/gex_ctl.sh install   render plist from template, bootstrap into launchd
#   deploy/gex_ctl.sh start|stop|restart
#   deploy/gex_ctl.sh status    launchd state + /health
#   deploy/gex_ctl.sh logs      tail service logs
#   deploy/gex_ctl.sh uninstall
#
# Run as the login user that owns IB Gateway. Never run with sudo.

set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
LABEL="com.gex.service"
PLIST_TEMPLATE="$PROJECT_ROOT/deploy/$LABEL.plist.template"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$PROJECT_ROOT/deploy/runtime"
DOMAIN="gui/$(id -u)"
ENV_FILE="$PROJECT_ROOT/.env"

api_port() {
  if [[ -f "$ENV_FILE" ]]; then
    local p
    p=$(grep -E '^GEX_API_PORT=' "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    [[ -n "$p" ]] && { echo "$p"; return; }
  fi
  echo 8090
}

render_plist() {
  mkdir -p "$LOG_DIR" "$(dirname "$PLIST_PATH")"
  sed -e "s#__PROJECT_ROOT__#$PROJECT_ROOT#g" \
      -e "s#__LOG_DIR__#$LOG_DIR#g" \
      "$PLIST_TEMPLATE" > "$PLIST_PATH"
  echo "rendered $PLIST_PATH"
}

ensure_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "missing $ENV_FILE; copy .env.example and adjust first" >&2
    exit 1
  fi
  if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    echo "missing $PROJECT_ROOT/.venv; run deploy/bootstrap_venv.sh first" >&2
    exit 1
  fi
}

is_loaded() {
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

cmd_install() {
  ensure_env
  render_plist
  if is_loaded; then
    launchctl bootout "$DOMAIN/$LABEL" || true
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
  launchctl enable "$DOMAIN/$LABEL"
  echo "installed and started $LABEL"
}

cmd_uninstall() {
  if is_loaded; then
    launchctl bootout "$DOMAIN/$LABEL"
  fi
  rm -f "$PLIST_PATH"
  echo "removed $LABEL"
}

cmd_start() {
  if ! is_loaded; then
    cmd_install
  else
    launchctl kickstart "$DOMAIN/$LABEL"
  fi
}

cmd_stop() {
  if is_loaded; then
    launchctl bootout "$DOMAIN/$LABEL"
    echo "stopped $LABEL (unloaded; use start to load again)"
  else
    echo "$LABEL not loaded"
  fi
}

cmd_restart() {
  if is_loaded; then
    launchctl kickstart -k "$DOMAIN/$LABEL"
  else
    cmd_install
  fi
}

cmd_status() {
  if is_loaded; then
    launchctl print "$DOMAIN/$LABEL" | grep -E 'state|pid|last exit' || true
  else
    echo "$LABEL not loaded"
  fi
  local port
  port=$(api_port)
  if command -v curl >/dev/null; then
    echo "--- http://127.0.0.1:$port/health"
    curl -fsS --max-time 5 "http://127.0.0.1:$port/health" || echo "(health endpoint unreachable)"
    echo
  fi
}

cmd_logs() {
  mkdir -p "$LOG_DIR"
  touch "$LOG_DIR/gex-service.log" "$LOG_DIR/gex-service-error.log"
  tail -n 100 -f "$LOG_DIR/gex-service.log" "$LOG_DIR/gex-service-error.log"
}

case "${1:-}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  *) echo "usage: $0 {install|uninstall|start|stop|restart|status|logs}" >&2; exit 2 ;;
esac
