#!/bin/bash
# loxklipper - install the Loxone G-code module into Klipper.
#
# Safe to run repeatedly. Moonraker's update manager runs this file
# unattended after every pull, so it never prompts and never blocks.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="loxone.py"
SRC="${REPO_DIR}/extras/${MODULE}"

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
KLIPPER_CONFIG="${KLIPPER_CONFIG:-}"
RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

info()  { echo "  $*"; }
ok()    { echo "  OK   $*"; }
warn()  { echo "  WARN $*"; }
die()   { echo "  FAIL $*" >&2; exit 1; }

echo "loxklipper installer"
echo "  repo: ${REPO_DIR}"

# --- never install as root -------------------------------------------------
# The symlink and the config file have to stay readable and writable by the
# user Klipper runs as; installing as root silently breaks both.
if [[ ${EUID} -eq 0 ]]; then
    die "run this as the user Klipper runs as (e.g. 'pi'), not as root."
fi

[[ -f "${SRC}" ]] || die "${SRC} not found - is the repository complete?"

# --- must be a git clone ---------------------------------------------------
# Moonraker's update manager drives a "git_repo" entry with git itself. A ZIP
# download has no .git, so Moonraker rejects the entry on startup and it never
# shows up in Mainsail - with the module itself working fine, which makes it a
# confusing failure. Catch it here instead.
IS_GIT=1
if ! git -C "${REPO_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
    IS_GIT=0
    warn "${REPO_DIR} is not a git clone (downloaded as ZIP?)."
    warn "The module will work, but Moonraker cannot auto-update it and the"
    warn "entry will not appear in Mainsail. To fix:"
    warn "  rm -rf ${REPO_DIR} && git clone https://github.com/sorglos-it/loxklipper.git ${REPO_DIR}"
fi

# --- locate Klipper --------------------------------------------------------
if [[ ! -d "${KLIPPER_PATH}/klippy/extras" ]]; then
    for candidate in "${HOME}/klipper" /usr/share/klipper /opt/klipper; do
        if [[ -d "${candidate}/klippy/extras" ]]; then
            KLIPPER_PATH="${candidate}"
            break
        fi
    done
fi
[[ -d "${KLIPPER_PATH}/klippy/extras" ]] || die \
    "no Klipper installation found. Set KLIPPER_PATH, e.g.
       KLIPPER_PATH=/home/pi/klipper bash install.sh"
ok "klipper: ${KLIPPER_PATH}"

# --- link the module -------------------------------------------------------
DEST="${KLIPPER_PATH}/klippy/extras/${MODULE}"
if [[ -e "${DEST}" && ! -L "${DEST}" ]]; then
    die "${DEST} exists and is a real file, not a link.
       Remove it by hand if it is not needed, then run this again."
fi
ln -sfn "${SRC}" "${DEST}"
ok "linked ${DEST} -> ${SRC}"

# --- locate the config directory ------------------------------------------
if [[ -z "${KLIPPER_CONFIG}" ]]; then
    for candidate in "${HOME}/printer_data/config" "${HOME}/klipper_config" \
                     "${HOME}/config"; do
        if [[ -d "${candidate}" ]]; then
            KLIPPER_CONFIG="${candidate}"
            break
        fi
    done
fi

# --- Moonraker update block ------------------------------------------------
# Written only when moonraker.conf exists and has no block yet, so an edited
# block survives the next update.
if [[ -n "${KLIPPER_CONFIG}" && -f "${KLIPPER_CONFIG}/moonraker.conf" ]]; then
    MOONRAKER_CONF="${KLIPPER_CONFIG}/moonraker.conf"
    if grep -q "^\[update_manager loxklipper\]" "${MOONRAKER_CONF}"; then
        ok "moonraker update block already present"
    else
        cp "${MOONRAKER_CONF}" "${MOONRAKER_CONF}.loxklipper.bak"
        {
            echo ""
            echo "[update_manager loxklipper]"
            echo "type: git_repo"
            echo "path: ${REPO_DIR}"
            echo "origin: https://github.com/sorglos-it/loxklipper.git"
            echo "primary_branch: main"
            echo "managed_services: klipper"
            echo "install_script: install.sh"
        } >> "${MOONRAKER_CONF}"
        ok "added [update_manager loxklipper] to ${MOONRAKER_CONF}"
        info "backup: ${MOONRAKER_CONF}.loxklipper.bak"
        MOONRAKER_NEEDS_RESTART=1
    fi
else
    warn "no moonraker.conf found - add the [update_manager loxklipper]"
    warn "block from the README by hand if you want auto-updates."
    warn "Looked in: ${KLIPPER_CONFIG:-~/printer_data/config, ~/klipper_config, ~/config}"
    warn "Set KLIPPER_CONFIG=/path/to/config and run this again if it lives elsewhere."
fi

# --- restart services ------------------------------------------------------
# Only with a terminal and passwordless sudo. Under Moonraker there is no TTY,
# and 'managed_services: klipper' restarts Klipper anyway - prompting for a
# password there would hang the update.
#
# Moonraker only reads moonraker.conf at startup, so a freshly added
# [update_manager] block stays invisible in Mainsail until it restarts. That
# is the usual reason the entry "does not show up".
CAN_SUDO=0
if [[ ${RESTART} -eq 1 ]] && [[ -t 0 ]] && sudo -n true 2>/dev/null; then
    CAN_SUDO=1
fi

restart_service() {  # $1 = service name
    if [[ ${CAN_SUDO} -eq 1 ]] && sudo -n systemctl restart "$1" 2>/dev/null; then
        ok "$1 restarted"
        return 0
    fi
    warn "restart $1 yourself:  sudo systemctl restart $1"
    return 1
}

restart_service klipper || true
if [[ ${MOONRAKER_NEEDS_RESTART:-0} -eq 1 ]]; then
    restart_service moonraker || true
fi

echo
echo "Done. Add a [loxone ...] section to printer.cfg - see docs/example-printer.cfg"
echo "Check it without the printer first:"
echo "  python3 ${REPO_DIR}/tools/loxone_send.py ${KLIPPER_CONFIG:-~/printer_data/config}/printer.cfg <name>"
if [[ ${IS_GIT} -eq 0 ]]; then
    echo
    echo "Reminder: this is not a git clone, so loxklipper will NOT appear in"
    echo "Mainsail's update manager. Re-clone it to fix that."
fi
