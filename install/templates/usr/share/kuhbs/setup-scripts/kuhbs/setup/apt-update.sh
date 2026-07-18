# Update apt the same way Qubes OS does with its Qubes update utility


# KUHBS intentionally performs APT updates exactly like Qubes OS.
# Qubes updater command path:
# https://github.com/QubesOS/qubes-core-admin-linux/blob/582536ccd95d759c82e189102a50404b8c41d114/vmupdate/agent/source/apt/apt_cli.py
# Qubes default Python-APT path applies the same policy:
# https://github.com/QubesOS/qubes-core-admin-linux/blob/582536ccd95d759c82e189102a50404b8c41d114/vmupdate/agent/source/apt/apt_api.py
# Qubes makes every repository refresh error fatal here:
# https://github.com/QubesOS/qubes-core-agent-linux/blob/606baad17d585c6b108dd35facce06b289d7dbcc/package-managers/apt-conf-41error-on-any
export DEBIAN_FRONTEND=noninteractive
apt-get -o "Debug::NoLocking=true" -q -o "APT::Update::Error-Mode=any" update
apt-get -y \
    -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold \
    dist-upgrade

# Qubes avoids unsafe general autoremove and removes only obsolete kernel packages
obsolete_kernels=()
autoremove_plan="$(apt-get autoremove -s)"
while read -r action package _rest; do
    if [[ "$action" == "Remv" && "$package" == linux-image* ]]; then
        obsolete_kernels+=("$package")
    fi
done <<< "$autoremove_plan"

if (( ${#obsolete_kernels[@]} )); then
    apt-get remove -y "${obsolete_kernels[@]}"
fi

apt-get clean
