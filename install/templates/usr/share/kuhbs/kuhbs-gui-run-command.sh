#!/bin/bash

# Run one KUHBS GUI command inside a visible dom0 terminal.
# Successful commands auto-close; failures keep the terminal useful for debugging.

set +e

success_color=$'\033[32m'
failure_color=$'\033[31m'
reset_color=$'\033[0m'

if [[ -n "${KUHBS_GUI_DISPLAY_COMMANDS:-}" ]]; then
    while IFS= read -r display_command; do
        echo "+ $display_command"
    done <<< "$KUHBS_GUI_DISPLAY_COMMANDS"
else
    display_args=("$@")
    display_args[0]="${display_args[0]##*/}"
    echo "+ ${display_args[*]}"
fi

"$@"
run_exit_status="$?"

if [[ "$run_exit_status" == "0" ]]; then
    echo -e "${success_color}\n\nExit status 0. This terminal will close automatically in 5 seconds. Press ENTER now to open a shell.${reset_color}"
    if read -r -t 5; then
        printf "%b" "$reset_color"
        /bin/bash -i
    fi
    printf "%b" "$reset_color"
    exit 0
fi

echo -e "${failure_color}\n\nCommand failed with exit code $run_exit_status${reset_color}"
if read -r -p "Press ENTER to start a shell, or CTRL + C to close this terminal"; then
    printf "%b" "$reset_color"
    /bin/bash -i
fi
printf "%b" "$reset_color"
exit "$run_exit_status"
