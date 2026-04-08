#!/bin/bash
# Control the TCE Admin systemd service.
#
# Usage:
#   ./scripts/tce-admin-service.sh start
#   ./scripts/tce-admin-service.sh stop
#   ./scripts/tce-admin-service.sh restart
#   ./scripts/tce-admin-service.sh status
#   ./scripts/tce-admin-service.sh logs

set -euo pipefail

SERVICE_NAME="tce-admin.service"

usage() {
    echo "Usage: $0 {start|stop|restart|status|logs}"
    exit 1
}

require_sudo() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "This command uses systemctl and usually needs sudo."
        echo "Run: sudo $0 $1"
        exit 1
    fi
}

ACTION="${1:-}"

case "$ACTION" in
    start)
        require_sudo "$ACTION"
        systemctl start "$SERVICE_NAME"
        systemctl status "$SERVICE_NAME" --no-pager
        ;;
    stop)
        require_sudo "$ACTION"
        systemctl stop "$SERVICE_NAME"
        systemctl status "$SERVICE_NAME" --no-pager || true
        ;;
    restart)
        require_sudo "$ACTION"
        systemctl restart "$SERVICE_NAME"
        systemctl status "$SERVICE_NAME" --no-pager
        ;;
    status)
        require_sudo "$ACTION"
        systemctl status "$SERVICE_NAME" --no-pager
        ;;
    logs)
        require_sudo "$ACTION"
        journalctl -u "$SERVICE_NAME" -n 100 --no-pager
        ;;
    *)
        usage
        ;;
esac
