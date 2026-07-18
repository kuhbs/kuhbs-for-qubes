# Install Element Desktop from the official Element Debian repository
# The keyring and source list are explicit so apt trusts only this repository for Element

apt-get -y install wget apt-transport-https
wget -O /usr/share/keyrings/element-io-archive-keyring.gpg https://packages.element.io/debian/element-io-archive-keyring.gpg
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/element-io-archive-keyring.gpg] http://packages.element.io/debian/ default main' > /etc/apt/sources.list.d/element-io.list
apt-get update
apt-get -y install element-desktop
