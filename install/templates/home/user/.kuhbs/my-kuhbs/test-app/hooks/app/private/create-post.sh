#!/bin/bash
set -e -x

# Restrict Element to the public matrix.org homeserver over HTTPS
# Voice/video TURN ports are intentionally not opened until this kuhb needs them
qvm-firewall app-test-app-private reset
qvm-firewall app-test-app-private add accept dsthost=matrix.org proto=tcp dstports=443
qvm-firewall app-test-app-private add drop

# Element i3 routing is written once by hooks/app/create-post.sh
echo "running hooks/app/private/create-post.sh"
