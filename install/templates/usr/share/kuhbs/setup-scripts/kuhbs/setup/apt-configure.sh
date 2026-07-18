# Configure apt for kuhbs in new TemplateVMs
# - Adjust network timeouts so they work via apt-cacher-ng while the user confirms apt-cacher-ng's requests to new repos in the Qubes-Snitch firewall
# - Adjust sources.list entries to use apt-cacher-ng's http://HTTPS/// syntax, so packages can be cached but connections to repos are done with https:// by apt-cacher-ng
# - Disable Qubes OS default apt upgrade checks, which can leak traffic in VMs behind kuhbs-net-firewall, also we don't need it, kuhbs does the upgrades, not Qubes



# Let one proxy request wait while DNS and TCP decisions are answered in Qubes Snitch
tee /etc/apt/apt.conf.d/80kuhbs-network-timeouts >/dev/null <<'EOF'
Acquire::http::Timeout "130";
Acquire::https::Timeout "130";
Acquire::Retries "0";
EOF


# Convert apt https:// entries to apt-cacher-ng HTTPS remaps
if test -f /etc/apt/sources.list; then
    sed -i 's@ https://@ http://HTTPS///@g' /etc/apt/sources.list
fi
# Guard globs because setup runs under set -e and minimal templates may omit either file shape
if compgen -G '/etc/apt/sources.list.d/*list' > /dev/null; then
    sed -i 's@ https://@ http://HTTPS///@g' /etc/apt/sources.list.d/*list
fi
if compgen -G '/etc/apt/sources.list.d/*sources' > /dev/null; then
    sed -i 's@ https://@ http://HTTPS///@g' /etc/apt/sources.list.d/*sources
fi


# Disable Debian/Qubes apt background checks in KUHBS-managed VMs
# KUHBS handles package upgrades explicitly via kuhbs upgrade and upgrade-all
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service
systemctl mask apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service

tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
