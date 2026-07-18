# Install baseline packages and terminal config used by KUHBS-managed VMs



# Install every packaged locale so app VMs can use normal user locale settings
DEBIAN_FRONTEND=noninteractive apt-get --yes install locales-all
locale-gen en_US.UTF-8

# Install common troubleshooting and terminal packages used by setup scripts and visible debug shells
apt-get --yes install \
  apt-file \
  curl \
  wget \
  zstd \
  locate \
  xfce4-terminal \
  tree \
  man-db
#  snapd \
# snapd stays disabled unless a KUHB explicitly needs it

# Update apt-file after installation so package-content lookups work immediately
apt-file update

# Install matching root and user xfce4-terminal config for visible KUHBS setup windows
install --directory --owner=root --group=root --mode=0755 /root/.config/xfce4/xfconf/xfce-perchannel-xml
install --owner=root --group=root --mode=0644 \
  /home/user/QubesIncoming/dom0/templates/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml \
  /root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml
rm --force /root/.config/xfce4/terminal/terminalrc

install --directory --owner=user --group=user --mode=0755 /home/user/.config/xfce4
install --directory --owner=user --group=user --mode=0755 /home/user/.config/xfce4/xfconf
install --directory --owner=user --group=user --mode=0755 /home/user/.config/xfce4/xfconf/xfce-perchannel-xml
install --owner=user --group=user --mode=0644 \
  /home/user/QubesIncoming/dom0/templates/home/user/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml \
  /home/user/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml
rm --force /home/user/.config/xfce4/terminal/terminalrc
