#!/usr/bin/env bash
# cron_setup.sh — Install a crontab entry for the LSPC-to-NBLM pipeline.
#
# Usage:
#   bash cron_setup.sh [--dry-run] [--time HH:MM]
#
# Options:
#   --dry-run   Print what would be installed without modifying crontab
#   --time      Set the daily run time (default: 09:00)

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve absolute paths
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# Detect Python: prefer project venv, fall back to system python3
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON_PATH="$PROJECT_DIR/.venv/bin/python"
elif [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
    PYTHON_PATH="$PROJECT_DIR/venv/bin/python"
else
    PYTHON_PATH="$(command -v python3 2>/dev/null || true)"
    if [[ -z "$PYTHON_PATH" ]]; then
        echo "ERROR: No Python 3 interpreter found." >&2
        echo "  Create a venv in the project directory or install python3." >&2
        exit 1
    fi
    # Resolve to absolute path
    PYTHON_PATH="$(readlink -f "$PYTHON_PATH")"
fi

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

DRY_RUN=0
RUN_TIME="09:00"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --time)
            if [[ -z "${2:-}" ]]; then
                echo "ERROR: --time requires a HH:MM argument." >&2
                exit 1
            fi
            RUN_TIME="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: bash cron_setup.sh [--dry-run] [--time HH:MM]"
            echo ""
            echo "Options:"
            echo "  --dry-run   Print what would be installed without modifying crontab"
            echo "  --time      Set the daily run time (default: 09:00)"
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate time format
if ! [[ "$RUN_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    echo "ERROR: Invalid time format '$RUN_TIME'. Expected HH:MM (00:00-23:59)." >&2
    exit 1
fi

HOUR="${RUN_TIME%%:*}"
MINUTE="${RUN_TIME##*:}"

# Strip leading zeros for cron (cron doesn't require them, but be safe)
HOUR="$((10#$HOUR))"
MINUTE="$((10#$MINUTE))"

# ---------------------------------------------------------------------------
# Build the crontab line
# ---------------------------------------------------------------------------

LOG_DIR="$PROJECT_DIR/logs"
CRON_COMMAND="cd $PROJECT_DIR && $PYTHON_PATH -m src.pipeline >> $LOG_DIR/cron.log 2>&1"
CRON_LINE="$MINUTE $HOUR * * * $CRON_COMMAND"
# Marker comment so we can detect duplicates
CRON_MARKER="# lspc-to-nblm pipeline"

# ---------------------------------------------------------------------------
# Display summary
# ---------------------------------------------------------------------------

echo "=== LSPC-to-NBLM Cron Setup ==="
echo ""
echo "Project directory : $PROJECT_DIR"
echo "Python interpreter: $PYTHON_PATH"
echo "Schedule          : daily at $RUN_TIME (cron: $MINUTE $HOUR * * *)"
echo "Log output        : $LOG_DIR/cron.log"
echo ""
echo "Crontab entry:"
echo "  $CRON_LINE $CRON_MARKER"
echo ""

# ---------------------------------------------------------------------------
# Dry-run: stop here
# ---------------------------------------------------------------------------

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] No changes made. Run without --dry-run to install."
    exit 0
fi

# ---------------------------------------------------------------------------
# Check for existing entry
# ---------------------------------------------------------------------------

EXISTING_CRONTAB="$(crontab -l 2>/dev/null || true)"

if echo "$EXISTING_CRONTAB" | grep -qF "lspc-to-nblm pipeline"; then
    echo "Existing lspc-to-nblm crontab entry detected:"
    echo "$EXISTING_CRONTAB" | grep "lspc-to-nblm pipeline"
    echo ""
    echo "Skipping installation to avoid duplicates."
    echo "To update, first remove the existing entry with: crontab -e"
    exit 0
fi

# ---------------------------------------------------------------------------
# Prompt for confirmation
# ---------------------------------------------------------------------------

read -r -p "Install this crontab entry? [y/N] " response
case "$response" in
    [yY][eE][sS]|[yY])
        ;;
    *)
        echo "Aborted."
        exit 0
        ;;
esac

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

# Ensure logs directory exists
mkdir -p "$LOG_DIR"

# Append to existing crontab
{
    echo "$EXISTING_CRONTAB"
    echo "$CRON_LINE $CRON_MARKER"
} | crontab -

echo "Crontab entry installed successfully."
echo "Verify with: crontab -l"
