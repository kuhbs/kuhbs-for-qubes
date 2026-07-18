#!/bin/bash
#
# Bootstrap KUHBS dom0 files, setup scripts, packages, and Python CLI/GUI
# Safe installer: does not shrink disks, create partitions, or enable storage services
# Desktop integration lives in the separate kuhbs-i3-integration repo

set -o errexit -o nounset -o pipefail
set -o xtrace



# Use paths relative to this installer directory
cd "$(dirname "$0")"


# Update dom0
#sudo qubes-dom0-update

# Install the dom0 packages used by the KUHBS CLI, GUI, and standard schema validator
#sudo qubes-dom0-update \
#    git \
#    gedit \
#    locate \
#    python3-pyyaml \
#    python3-jsonschema \
#    zenity \
#    zstd

# Update firmware (requires the fwupd-qubes-vm package to be installed in the updatevm)
#sudo qubes-fwupdmgr refresh
#sudo qubes-fwupdmgr update


# Prepare /home/user/.kuhbs directory
install --verbose --directory --mode=0755 /home/user/.kuhbs
install --verbose --directory --mode=0755 /home/user/.kuhbs/my-kuhbs
install --verbose --directory --mode=0755 /home/user/.kuhbs/repos

# Enable Gedit's built-in file tree so Edit shows every file in the selected KUHB
gsettings set org.gnome.gedit.plugins active-plugins "['filebrowser']"
gsettings set org.gnome.gedit.preferences.ui side-panel-visible true
gsettings set org.gnome.gedit.preferences.editor scheme 'oblivion'
gsettings set org.gnome.gedit.plugins.filebrowser tree-view true
gsettings set org.gnome.gedit.plugins.filebrowser open-at-first-doc true
gsettings set org.gnome.gedit.state.window side-panel-active-page 'GeditFileBrowserPanel'

# Install user-owned KUHBS custom setup scripts and definitions only; desktop config is separate
install --verbose --directory --mode=0755 /home/user/.kuhbs/setup-scripts
if ! test -e /home/user/.kuhbs/setup-scripts/example-user-script.sh; then
    cp --verbose --archive templates/home/user/.kuhbs/setup-scripts/example-user-script.sh /home/user/.kuhbs/setup-scripts/example-user-script.sh
fi

# Install dom0 xfce4-terminal config for root and user
sudo install --verbose --directory --owner=root --group=root --mode=0755 /root/.config/xfce4
sudo install --verbose --directory --owner=root --group=root --mode=0755 /root/.config/xfce4/xfconf
sudo install --verbose --directory --owner=root --group=root --mode=0755 /root/.config/xfce4/xfconf/xfce-perchannel-xml
sudo install --verbose --owner=root --group=root --mode=0644 \
    templates/usr/share/kuhbs/setup-scripts/kuhbs/setup/templates/root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml \
    /root/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml
sudo rm --verbose --force /root/.config/xfce4/terminal/terminalrc

install --verbose --directory --owner=user --group=user --mode=0755 /home/user/.config/xfce4
install --verbose --directory --owner=user --group=user --mode=0755 /home/user/.config/xfce4/xfconf
install --verbose --directory --owner=user --group=user --mode=0755 /home/user/.config/xfce4/xfconf/xfce-perchannel-xml
install --verbose --owner=user --group=user --mode=0644 \
    templates/usr/share/kuhbs/setup-scripts/kuhbs/setup/templates/home/user/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml \
    /home/user/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-terminal.xml
rm --verbose --force /home/user/.config/xfce4/terminal/terminalrc




# Install the Debian 13 minimal template used by most bundled KUHB definitions
#qvm-template install debian-13-minimal

# Install the kicksecure template
#qvm-template --enablerepo qubes-templates-community install kicksecure-18

# Disable Qubes prewarmed DisposableVMs so KUHBS launchers start them only when needed
qvm-features dom0 default_dispvm_prewarm 0
qvm-prefs debian-13-minimal label purple
qvm-prefs whonix-gateway-18 label purple
qubes-prefs default-dispvm default-dvm


# Disable Qubes LVM revision snapshots so KUHBS cleanup and disk splitting do not fight hidden revision volumes
qvm-pool set vm-pool --option revisions_to_keep=0
qvm-pool info vm-pool


# Make boot logs visible by removing the quiet graphical boot flags
#sudo sed --in-place 's/ rhgb quiet//g' /etc/grub2-efi.cfg
#sudo sed --in-place 's/ rhgb quiet//g' /etc/default/grub
#sudo grub2-mkconfig --output /boot/efi/EFI/qubes/grub.cfg

# Install the KUHBS Python package, defaults, executable wrappers, and shell integration
sudo install --verbose --directory --mode=0755 /usr/share/kuhbs
sudo install --verbose --directory --mode=0755 /etc/bash_completion.d
sudo rm --verbose --recursive --force /usr/share/kuhbs/kuhbs
sudo rm --verbose --recursive --force /usr/share/kuhbs/schemas
sudo rm --verbose --recursive --force /usr/share/kuhbs/setup-scripts
sudo rm --verbose --recursive --force /usr/share/kuhbs/launcher-icons
sudo rm --verbose --recursive --force /usr/share/kuhbs/icons
sudo install --verbose --directory --owner=root --group=root --mode=0755 /usr/share/kuhbs/kuhbs
sudo cp --verbose --recursive --no-preserve=ownership ../kuhbs/. /usr/share/kuhbs/kuhbs
sudo install --verbose --directory --owner=root --group=root --mode=0755 /usr/share/kuhbs/schemas
sudo install --verbose --owner=root --group=root --mode=0644 ../schemas/*.yml /usr/share/kuhbs/schemas/
sudo install --verbose --directory --owner=root --group=root --mode=0755 /usr/share/kuhbs/setup-scripts
sudo cp --verbose --recursive --no-preserve=ownership templates/usr/share/kuhbs/setup-scripts/. /usr/share/kuhbs/setup-scripts
sudo install --verbose --directory --owner=root --group=root --mode=0755 /usr/share/kuhbs/launcher-icons
sudo cp --verbose --recursive --no-preserve=ownership templates/usr/share/kuhbs/launcher-icons/. /usr/share/kuhbs/launcher-icons
# Synthetic GUI rows have no KUHB directory, so their bundled icons need a shared installed location
sudo install --verbose --directory --owner=root --group=root --mode=0755 /usr/share/kuhbs/icons
sudo cp --verbose --recursive --no-preserve=ownership templates/usr/share/kuhbs/icons/. /usr/share/kuhbs/icons
sudo install --verbose --mode=0644 ../defaults.yml /usr/share/kuhbs/defaults.yml
sudo install --verbose --owner=root --group=root --mode=0755 templates/usr/share/kuhbs/kuhbs-repo-operation.sh /usr/share/kuhbs/kuhbs-repo-operation.sh
sudo install --verbose --mode=0644 templates/usr/share/kuhbs/kuhbs.css /usr/share/kuhbs/kuhbs.css
sudo install --verbose --mode=0755 templates/usr/share/kuhbs/kuhbs-gui-run-command.sh /usr/share/kuhbs/kuhbs-gui-run-command.sh
sudo install --verbose --mode=0755 templates/usr/bin/kuhbs /usr/bin/kuhbs
sudo install --verbose --mode=0755 templates/usr/bin/kuhbs-gui /usr/bin/kuhbs-gui
sudo install --verbose --mode=0644 templates/etc/bash_completion.d/kuhbs /etc/bash_completion.d/kuhbs


# Update locate db
#sudo updatedb


# Finished
echo 'KUHBS install complete'
