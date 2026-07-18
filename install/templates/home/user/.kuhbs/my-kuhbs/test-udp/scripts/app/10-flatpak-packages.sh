# Install Zen Browser as a user Flatpak in the disposable template AppVM
# Flathub is scoped to the user so the browser payload stays out of dom0

su -l user -c "flatpak --user remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo"
su -l user -c "flatpak --user install --verbose --assumeyes --noninteractive flathub app.zen_browser.zen"
