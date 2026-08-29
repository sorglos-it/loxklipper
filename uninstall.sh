#!/bin/bash
# loxklipper - remove the module link from Klipper.
#
# Leaves printer.cfg and moonraker.conf alone; both are edited by hand and
# an installer has no business deleting user configuration.

set -euo pipefail

MODULE="loxone.py"
KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"

if [[ ! -d "${KLIPPER_PATH}/klippy/extras" ]]; then
    for candidate in "${HOME}/klipper" /usr/share/klipper /opt/klipper; do
        if [[ -d "${candidate}/klippy/extras" ]]; then
            KLIPPER_PATH="${candidate}"
            break
        fi
    done
fi

DEST="${KLIPPER_PATH}/klippy/extras/${MODULE}"
if [[ -L "${DEST}" ]]; then
    rm -f "${DEST}"
    echo "  OK   removed ${DEST}"
elif [[ -e "${DEST}" ]]; then
    echo "  WARN ${DEST} is a real file, not a link - left untouched" >&2
else
    echo "  OK   nothing to remove"
fi

echo
echo "Still to do by hand:"
echo "  - remove the [loxone ...] sections from printer.cfg"
echo "  - remove [update_manager loxklipper] from moonraker.conf"
echo "  - sudo systemctl restart klipper"
