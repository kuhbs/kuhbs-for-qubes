#!/bin/bash
set -euo pipefail

# Removing the Zen kuhb removes only its i3 drop-in file.
# Other KUHBS and hand-written i3 rules stay untouched.
rm -f ~/.config/i3/config.d/kuhbs-zen.conf

i3-msg reload
