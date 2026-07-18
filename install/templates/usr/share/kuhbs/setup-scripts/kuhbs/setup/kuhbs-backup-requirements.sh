# Install the qrexec services used by the KUHBS backup VM



# BackupWrite receives archive bytes from a source VM and stores them under the caller VM name
install --owner=root --group=root --mode=0755 \
  /home/user/QubesIncoming/dom0/templates/etc/qubes-rpc/kuhbs.BackupWrite \
  /etc/qubes-rpc/kuhbs.BackupWrite

# BackupRead serves the archive matching the caller VM name during pull-based restore
install --owner=root --group=root --mode=0755 \
  /home/user/QubesIncoming/dom0/templates/etc/qubes-rpc/kuhbs.BackupRead \
  /etc/qubes-rpc/kuhbs.BackupRead
