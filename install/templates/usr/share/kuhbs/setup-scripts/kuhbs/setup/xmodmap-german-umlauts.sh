# Map the right Alt key as the Mode_switch key for German umlauts


cat << 'EOF' | tee /home/user/.Xmodmap
keycode 108 = Mode_switch
keysym e = e E EuroSign
keysym a = a A adiaeresis Adiaeresis
keysym o = o O odiaeresis Odiaeresis
keysym u = u U udiaeresis Udiaeresis
keysym s = s S ssharp
EOF

# User session must be able to read the file written by root setup
chown user:user /home/user/.Xmodmap
chmod 644 /home/user/.Xmodmap
