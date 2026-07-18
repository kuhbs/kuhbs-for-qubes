#!/bin/bash

# Run one KUHBS hook or setup script inside its dedicated visible terminal
# Write the real script status before any prompt because terminal emulators expose only UI lifetime
# ENTER opens a debug shell; CTRL+C closes the terminal by normal shell behavior

set +e

exit_status_file="$1"
run_script="$2"
shift 2
success_color=$'\033[32m'
failure_color=$'\033[31m'
reset_color=$'\033[0m'

# Keep setup output visible, stop on first failure, and pass optional args to the script
/bin/bash -e -x "$run_script" "$@"
run_exit_status="$?"

# Publish only a complete executable marker because the parent polls the final path concurrently
umask 0277
exit_status_tmp="${exit_status_file}.tmp.$$"
printf '#!/bin/bash\nexit %s\n' "$run_exit_status" > "$exit_status_tmp"
chmod 500 "$exit_status_tmp"
mv -f -- "$exit_status_tmp" "$exit_status_file"

# Successful runs close automatically unless the user explicitly asks for a shell
if [[ "$run_exit_status" == "0" ]]; then
    echo -e "${success_color}\n\nExit status 0. This terminal will close automatically in 5 seconds. Press ENTER now to open a shell.${reset_color}"
    if read -r -t 5; then
        printf "%b" "$reset_color"
        /bin/bash -i
    fi
    printf "%b" "$reset_color"
    exit 0
fi

# Failed runs offer a shell after writing the status file; CTRL+C just closes the wrapper
# The original failure still propagates if the user exits the shell normally
echo -e "${failure_color}\n\nScript failed with exit code $run_exit_status${reset_color}"
if read -r -p "Press ENTER to start a shell, or CTRL + C to close this terminal"; then
    printf "%b" "$reset_color"
    /bin/bash -i
fi
printf "%b" "$reset_color"
exit "$run_exit_status"
