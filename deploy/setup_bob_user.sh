#!/bin/sh
# Creates the separate standard macOS user `bob` that bobd and its plugin servers run as,
# so the OS itself stops Bob writing to your home folder or ~/dev.
#
# This changes system settings and needs your admin password. Read it, then run it
# yourself:   sudo sh deploy/setup_bob_user.sh /path/to/Bob
#
# Afterwards set `security.os_isolation: required` in config.yaml. From then on bobd
# shuts down if the conformance test finds it can write into a protected folder.
set -eu

BOB_HOME=${1:?usage: sudo sh deploy/setup_bob_user.sh /path/to/Bob}
OWNER=${SUDO_USER:?run with sudo from your own account}
OWNER_HOME=$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')

if ! id bob >/dev/null 2>&1; then
    sysadminctl -addUser bob -fullName "Bob Agent" -shell /usr/bin/false -home /Users/bob
    # A service account: it never appears on the login screen.
    dscl . -create /Users/bob IsHidden 1
fi

# What bob may write: its own data, the knowledge base, and plugin output folders.
for dir in "$BOB_HOME/data" "$OWNER_HOME/knowledge_base" "$OWNER_HOME/bob_output"; do
    mkdir -p "$dir"
    chown -R bob:staff "$dir"
    chmod -R u+rwX,g+rX,o-rwx "$dir"
done

# What bob may read: the code it runs. Nothing else in your home folder is opened to it.
chmod -R g+rX "$BOB_HOME"
chmod +a "bob allow search" "$OWNER_HOME" 2>/dev/null || true

# ~/dev stays unreadable and unwritable for bob: no ACL is added for it.
echo "User 'bob' is ready. Install deploy/launchd/com.bob.bobd.plist as a LaunchDaemon"
echo "with <key>UserName</key><string>bob</string>, then set os_isolation: required."
