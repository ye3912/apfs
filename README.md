# APFS Data seed for UniXNU 21H461

Run **APFS Data seed (21H461)** from Actions. It fetches only the public Apple
IPSW's pinned OS member, verifies the original launchd and initial `/private/var`
against an independently extracted file/metadata inventory, then uses macOS
`hdiutil` to create a 256 MiB APFS volume. Original permissions, ownership and
symlinks are preserved; `staged_system_apps` is excluded. Runtime directories
are added without generating keys or pre-creating tzinit's timezone symlink.

Apple `fsck_apfs` checks the detached filesystem before DarwinBoot's pinned
`DBRDISK1` packer removes zero pages. The three-day artifact contains the packed
Data seed, its UUID/hash report, expected inventory and filesystem check log.
It contains factory OS defaults, and no personal device Data or private keys.

This is guest-writable RAM Data. It does not provide persistence or keybag
provisioning. Native mount and userspace startup must be verified on the target.

References: https://unixnu.com/building and https://unixnu.com/guide/userdata.
