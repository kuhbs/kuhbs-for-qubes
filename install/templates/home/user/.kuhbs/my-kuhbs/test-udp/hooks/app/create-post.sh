#!/bin/bash
set -euo pipefail

# Optional i3 integration writes one drop-in file for the whole Zen kuhb.
# The i3 integration installer owns ~/.config/i3/config.d and the include line.
cat > ~/.config/i3/config.d/kuhbs-zen.conf <<'EOF'
# KUHBS Zen routing
assign [class="zen"] "2|SURF"
for_window [title="^app-test-udp: "] move to workspace "2|SURF"
EOF

i3-msg reload
