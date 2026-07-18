#!/bin/bash
set -euo pipefail

# Optional i3 integration writes one drop-in file for the whole Element kuhb.
# The i3 integration installer owns ~/.config/i3/config.d and the include line.
cat > ~/.config/i3/config.d/kuhbs-element.conf <<'EOF'
# KUHBS Element routing
assign [class="Element"] "22|CHAT"
assign [class="element"] "22|CHAT"
for_window [title="^app-test-app-.*: "] move to workspace "22|CHAT"
EOF

i3-msg reload
