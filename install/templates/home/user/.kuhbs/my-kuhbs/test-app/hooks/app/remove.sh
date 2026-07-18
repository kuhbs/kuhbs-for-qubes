#!/bin/bash
set -euo pipefail

# Removing the Element kuhb removes only its i3 drop-in file.
# Other KUHBS and hand-written i3 rules stay untouched.
rm -f ~/.config/i3/config.d/kuhbs-element.conf

i3-msg reload
