# system-administration

# System Administration (Omarchy)

Manage Linux system administration tasks on Omarchy (Arch-based) systems.

## When This Skill MUST Be Used

**ALWAYS invoke this skill for end-user requests involving:**

- Disk space analysis (`df`, `lsblk`, partition layout)
- Partition management (resizing, merging, expanding)
- LVM detection and handling
- Encrypted filesystem inspection (LUKS, cryptsetup)
- Drive health checks (`smartctl`, disk diagnostics)
- Mount point management and fstab editing
- Swap space analysis and configuration

## Disk Inspection Workflow

### 1. Initial Layout Analysis

Always start with a high-level view of the disk layout:

```bash
# Show all block devices and their mount points
lsblk

# Detailed partition table
lsblk -f  # includes filesystem types and labels

# Check mounted filesystems
mount | grep -v "proc\|sysfs\|cgroup\|tmpfs\|devpts\|mqueue"
```

Example output interpretation:
```
NAME   MAJ:MIN RM   SIZE RO TYPE  MOUNTPOINTS
sda      8:0    0 238.5G  0 disk
├─sda1   8:1    0   600M  0 part
├─sda2   8:2    0     2G  0 part
├─sda3   8:3    0   183G  0 part  # Potential expansion candidate
├─sda4   8:4    0     2G  0 part  /boot
└─sda5   8:5    0  50.9G  0 part  / # encrypted LVM
```

### 2. Detect LVM vs Traditional Partitions

Check for LVM tooling availability:

```bash
# These commands should exist if LVM is in use
which pvs vgs lvs 2>/dev/null

# If any exist and return output, the system uses LVM
pvs  # Physical volumes
vgs  # Volume groups
lvs  # Logical volumes

# If commands are missing (command not found), assume traditional partitioning
```

**Key indicators:**
- **LVM present**: `pvs`, `vgs`, `lvs` produce output → use `lvextend`
- **LVM absent**: `pvs: command not found` → traditional partitions

### 3. Identify Encrypted Filesystems

```bash
# Check for LUKS encryption
blkid /dev/sda5

# Typical output for encrypted partition:
# /dev/sda5: LUKS1 uuid="..." 

# Check what's currently decrypted and in use
cat /proc/mounts | grep crypt
ls /dev/mapper/

# Look for crypttab entries
cat /etc/crypttab
```

If you see entries like `omarchy_root` in `/dev/mapper/`, the partition is LUKS-encrypted with a mapped device.

### 4. Filesystem Types and Usage

```bash
# Check available space with human-readable format
df -h

# Show filesystem types
df -Th

# Detailed filesystem info for specific device
blkid /dev/sda5
file -s /dev/sda5
```

## Safe Partition Expansion Guidelines

### Before Any Expansion

1. **Backup critical data** - Always ensure backups exist before partition operations
2. **Verify system integrity** - `dmesg` output, no pending I/O errors
3. **Check SMART health** - `smartctl -a /dev/sda` (if available)
4. **Confirm target partition is safe to modify** - never touch active partitions without proper unmounting

### Identifying Expansion Candidates

Look for:
- Unallocated space on the disk (`lsblk -f` shows gaps)
- Partitions that are not mounted and not in use
- Partitions with no filesystem signature (`blkid` returns nothing)

**Warning signs to avoid:**
- Partitions you cannot read (`file -s /dev/sdX` returns "no read permission")
- Partitions actively mounted (check `/proc/mounts`)
- Partitions that are part of active RAID arrays
- Swap or hibernation partitions

### Safe Expansion Procedure (High-Level)

1. **Analyze current state** - Document partition table (`sfdisk -d /dev/sda`)
2. **Shrink/resize safely** - Unmount target partition, resize filesystem
3. **Handle encryption layers** - If LUKS, resize both LUKS container and inner filesystem
4. **Extend filesystem** - `resize2fs` for ext4, `xfs_growfs` for XFS
5. **Update /etc/fstab** if mount points changed
6. **Verify** - `df -h`, `mount`, and reboot test

## Common Scenarios

### Scenario: Expand Root Filesystem on Encrypted Partition

```bash
# Check current state
lsblk -f
df -h /

# If using LUKS (e.g., /dev/mapper/omarchy_root)
# Expand the underlying block device first (partition or LUKS container)
# Then resize the filesystem:
sudo cryptsetup resize omarchy_root
sudo resize2fs /dev/mapper/omarchy_root
```

### Scenario: Check Disk Health Before Expansion

```bash
# Install smartmontools if needed
omarchy pkg add smartmontools

# Check health
sudo smartctl -a /dev/sda
sudo smartctl -H /dev/sda
```

### Scenario: SSH Tunnel for Remote Service Access

```bash
# Forward remote port 9119 (Hermes dashboard) to localhost
ssh -N -L 9119:localhost:9119 root@192.168.1.11

# Verify tunnel works
curl http://localhost:9119

# Background with auto-reconnect
ssh -N -o ServerAliveInterval=60 -L 9119:localhost:9119 root@192.168.1.11
```

## Privilege Escalation

Use `sudo` for all privileged commands. For Omarchy systems, sudo is typically available and may be passwordless for certain commands.

**Avoid:** Directly editing `/etc/fstab` without understanding the implications. Always test with `mount -a` after changes.

## Troubleshooting

### Permission Denied Errors

```bash
# Check if commands exist
which pvs vgs lvs blkid smartctl

# Install missing tools
omarchy pkg add lvm2 e2fsprogs xfsprogs smartmontools

# Run with sudo when needed
sudo blkid /dev/sda5
```

### "No Read Permission" on Partitions

A `file -s /dev/sda3` returning "no read permission" means the kernel/user lacks access. Try:

```bash
sudo file -s /dev/sda3
sudo blkid /dev/sda3
```

If still empty, the partition may be uninitialized or protected.

### Checking Mount Status

```bash
# What's currently mounted
cat /proc/mounts

# Specifically check our target partitions
cat /proc/mounts | grep sda
```

## References

- [`references/disk-inspection-commands.md`](references/disk-inspection-commands.md) - Quick reference for all disk inspection commands
- [`references/partition-resize-checklist.md`](references/partition-resize-checklist.md) - Step-by-step pre-flight checklist for safe expansion
