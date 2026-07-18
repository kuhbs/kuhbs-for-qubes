# This script is executed with set -x, but the output is too spammy to understand this way
# You can read the source look :P
set +x
echo "Disabled the default set -x for this script only to improve readability."
echo -e "This script can be audited at kuhbs-for-qubes/install/templates/usr/share/kuhbs/setup-scripts/kuhbs/backup-mount.sh\n"

# Variables
mount_path="$1"
backup_path="$2"
crypt_name="$3"

# Show only likely USB disks so Qubes system volumes do not clutter the prompt
/usr/bin/lsblk --paths --nodeps --noheadings --output NAME,SIZE,MODEL,TYPE,TRAN | /usr/bin/grep usb || /bin/true

# Ask for correct storage device
while /bin/true; do
    read -r -p "Please enter the storage device name, for example sda: " device_name
    device="/dev/$device_name"
    # Refuse typos before cryptsetup or dd touches a path
    /usr/bin/test -b "$device" && break
    echo "$device is not a valid block device, try again"
done

# Ask if the user has already encrypted / formatted the disk before
while /bin/true; do
    read -r -p "Have you formatted this storage device with KUHBS backup-mount before? [y/n] " existing
    [[ "$existing" == "y" || "$existing" == "n" ]] && break
    echo "Please answer y or n"
done

# Try to decrypt an existing volume; correct passwords reveal the ext4 filesystem
try_decrypt_existing() {
    while /bin/true; do
        echo "Enter the encryption password"
        /usr/sbin/cryptsetup open --type plain --cipher aes-xts-plain64 --key-size 512 --hash sha512 "$device" "$crypt_name"
        # Headerless encryption cannot authenticate directly, so ext4 is the password check
        if /usr/sbin/blkid --output value --match-tag TYPE "/dev/mapper/$crypt_name" | /usr/bin/grep --quiet --line-regexp ext4; then
            return 0
        fi
        echo "That password was incorrect, try again"
        /usr/sbin/cryptsetup close "$crypt_name"
    done
}

# Existing disks should already contain the KUHBS ext4 filesystem
if [[ "$existing" == "y" ]]; then
    try_decrypt_existing
    /usr/bin/mount --verbose --options nodev,nosuid,noexec,noatime,errors=remount-ro "/dev/mapper/$crypt_name" "$mount_path"
    /usr/bin/install --directory --owner=user --group=user --mode=0700 "$backup_path"
    /usr/bin/chmod 700 "$backup_path"
    echo "Device is now mounted to $mount_path, backups will be created in $backup_path"
    exit 0
fi

# Formatting is destructive, so require the exact KUHBS confirmation phrase
echo "This will erase all data on $device"
read -r -p "Type FORMAT to continue: " confirmation
if [[ "$confirmation" != "FORMAT" ]]; then
    echo "Aborting"
    exit 1
fi

# Overwrite the disk with random data to hide the fact that there is encrypted data
while /bin/true; do
    read -r -p "Overwrite the entire storage device with random data first? This can take a long time. [y/n] " overwrite
    if [[ "$overwrite" == "y" ]]; then
        # shred knows the device size, so it exits cleanly instead of treating a full device as a dd error
        /usr/bin/shred --verbose --iterations=1 --random-source=/dev/urandom "$device"
        break
    fi
    [[ "$overwrite" == "n" ]] && break
    echo "Please answer y or n"
done

# Format through the first password, then reopen to prove the user can reproduce it
echo "Encrypting, formatting with ext4, mounting to $mount_path and then creating $backup_path"
/usr/sbin/cryptsetup open --type plain --cipher aes-xts-plain64 --key-size 512 --hash sha512 "$device" "$crypt_name"
/usr/sbin/mkfs.ext4 -L KUHBS_BACKUPS "/dev/mapper/$crypt_name"
/usr/bin/mount --verbose --options nodev,nosuid,noexec,noatime,errors=remount-ro "/dev/mapper/$crypt_name" "$mount_path"
/usr/bin/install --directory --owner=user --group=user --mode=0700 "$backup_path"
/usr/bin/umount --verbose "$mount_path"
/usr/sbin/cryptsetup close "$crypt_name"

# The second open proves the password before leaving the backup disk mounted
echo "Verifying encryption password"
try_decrypt_existing
/usr/bin/mount --verbose --options nodev,nosuid,noexec,noatime,errors=remount-ro "/dev/mapper/$crypt_name" "$mount_path"

# Verify the mounted filesystem still contains the expected backup dir before dom0 reports success
/usr/bin/test -d "$backup_path"
echo "Creation of plausible deniability encrypted device completed. Device is now mounted to $mount_path, backups will be created in $backup_path"
