#!/usr/bin/env python3
"""Create a mutable RAM Data seed using Apple's formatter and original 21H461 var.

Only public Apple IPSW input is fetched. No kernel, trust cache, private key,
personal Data volume, or complete IPSW is uploaded by the workflow.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile

LAUNCHD = '8cec9a3c44a443b08e977ccc5d577850316ad428c575dc01f09b018dbd5b6a25'
LOGICAL = 256 * 1024**2
SETUP_PREFERENCES = {
    'com.apple.purplebuddy.plist': {
        'SetupDone': True,
        'SetupFinishedAllSteps': True,
        'SetupVersion': 11,
        'daemonizeNeedsToRun': False,
    },
    'com.apple.purplebuddy.notbackedup.plist': {
        key: False for key in (
            'ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
            'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy',
            'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy', 'SilentICDPUpgrade',
        )
    },
}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run(*args):
    return subprocess.check_output(list(map(str, args)))


def attach(image, target, readonly):
    target.mkdir()
    command = ['hdiutil', 'attach', '-nobrowse', '-owners', 'on',
               '-mountpoint', str(target), '-plist']
    if readonly:
        command += ['-readonly']
    entities = plistlib.loads(run(*command, image))['system-entities']
    mounted = [v for v in entities if v.get('mount-point') == str(target)]
    if len(mounted) != 1:
        raise RuntimeError('Expected exactly one mounted volume in image')
    return entities[0]['dev-entry'], mounted[0]['dev-entry']


def validate_tree(root, entries):
    for entry in entries:
        rel = PurePosixPath(entry['path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Invalid expected path')
        path = root / rel
        value = path.lstat()
        expected = entry['type']
        actual = ('symlink' if stat.S_ISLNK(value.st_mode) else
                  'directory' if stat.S_ISDIR(value.st_mode) else
                  'file' if stat.S_ISREG(value.st_mode) else 'unsupported')
        if actual != expected or stat.S_IMODE(value.st_mode) != int(entry['mode'], 8):
            raise ValueError(f'Type/mode differs: {rel}')
        if (value.st_uid, value.st_gid) != (entry['uid'], entry['gid']):
            raise ValueError(f'Ownership differs: {rel}')
        if expected == 'file' and (value.st_size != entry['bytes'] or sha(path) != entry['sha256']):
            raise ValueError(f'Original content differs: {rel}')
        if expected == 'symlink' and os.readlink(path) != entry['target']:
            raise ValueError(f'Original symlink differs: {rel}')


def copy_tree(source, target, entries):
    # The source paths, kinds and contents were independently pinned on Linux.
    # Never traverse a symlink while creating children in the new filesystem.
    for entry in sorted(entries, key=lambda e: (len(PurePosixPath(e['path']).parts), e['path'])):
        src, dst = source / entry['path'], target / entry['path']
        if entry['type'] == 'directory':
            dst.mkdir(exist_ok=True)
        elif entry['type'] == 'symlink':
            dst.symlink_to(os.readlink(src))
        elif entry['type'] == 'file':
            shutil.copyfile(src, dst, follow_symlinks=False)
        else:
            raise ValueError('Unsupported seed entry')
        os.chown(dst, entry['uid'], entry['gid'], follow_symlinks=False)
        os.chmod(dst, int(entry['mode'], 8), follow_symlinks=False)
    validate_tree(target, entries)
    for name, mode, uid, gid in [('keybags', 0o700, 0, 0),
                                 ('mobile/tmp', 0o700, 501, 501),
                                 ('tmp', 0o1777, 0, 0), ('run', 0o755, 0, 0)]:
        p = target / name
        if not p.exists():
            p.mkdir(parents=True)
            os.chown(p, uid, gid)
            os.chmod(p, mode)
        if name == 'tmp':
            os.chmod(p, mode)
    # tzinit must perform its original symlink operation on the device.
    if (target / 'db/timezone/zoneinfo').is_symlink() or (target / 'db/timezone/zoneinfo').exists():
        raise ValueError('Do not pre-create tzinit output')


def write_setup_preferences(root):
    preferences = root / 'mobile/Library/Preferences'
    records = []
    for filename, values in SETUP_PREFERENCES.items():
        path = preferences / filename
        with path.open('xb') as stream:
            plistlib.dump(values, stream, fmt=plistlib.FMT_BINARY)
        os.chown(path, 501, 501, follow_symlinks=False)
        os.chmod(path, 0o600, follow_symlinks=False)
        if plistlib.loads(path.read_bytes()) != values:
            raise ValueError(f'Setup state readback failed for {filename}')
        records.append({'path': str(path.relative_to(root)), 'mode': '0o600',
                        'uid': 501, 'gid': 501, 'type': 'file',
                        'bytes': path.stat().st_size, 'sha256': sha(path), 'values': values})
    validate_tree(root, records)
    if any((root / 'keybags').iterdir()):
        raise ValueError('Factory seed must have no keybag')
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unixnu', type=Path, required=True)
    parser.add_argument('--darwinboot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    a = parser.parse_args()
    if platform.system() != 'Darwin' or os.geteuid() != 0:
        raise SystemExit('Requires macOS runner and sudo for original ownership')
    out = a.output.resolve()
    out.mkdir(exist_ok=False)
    sys.path.insert(0, str(a.unixnu.resolve() / 'scripts'))
    from unixnu.ipsw import RangeFile
    sys.path.insert(0, str(a.darwinboot.resolve() / 'Test'))
    from packed_ramdisk import pack
    profile = tomllib.loads((a.unixnu / 'profiles/os/ipados-17.7.11-21H461/profile.toml').read_text())['ipsw']
    stream = RangeFile(profile['url'])
    archive = zipfile.ZipFile(stream)
    manifest = plistlib.loads(archive.read('BuildManifest.plist'))
    if (manifest['ProductVersion'], manifest['ProductBuildVersion']) != ('17.7.11', '21H461'):
        raise ValueError('Wrong IPSW build')
    if 'iPad7,1' not in manifest['SupportedProductTypes']:
        raise ValueError('Wrong IPSW product')
    identity = [i for i in manifest['BuildIdentities']
                if i['Info']['DeviceClass'] == 'j120ap' and i['Info']['RestoreBehavior'] == 'Erase'
                and int(i['ApChipID'], 16) == 32785 and int(i['ApBoardID'], 16) == 12]
    if len(identity) != 1:
        raise ValueError('Require unique original erase identity')
    member = identity[0]['Manifest']['OS']['Info']['Path']
    expected_hash = profile['sha256'][Path(member).name]
    dmg = out / 'original-os.dmg'
    print(f'Fetching only pinned OS member {member}', flush=True)
    with archive.open(member) as source, dmg.open('xb') as dest:
        shutil.copyfileobj(source, dest, 8 << 20)
    if sha(dmg) != expected_hash:
        raise ValueError('OS DMG hash mismatch')
    archive.close()
    expected = json.loads(Path(__file__).resolve().with_name('expected-var.json').read_text())
    os_device, _ = attach(dmg, out / 'os-mount', True)
    raw = out / 'data-21H461.cdr'
    try:
        os_mount = out / 'os-mount'
        if sha(os_mount / 'sbin/launchd') != LAUNCHD:
            raise ValueError('Original launchd pin mismatch')
        source = os_mount / 'private/var'
        validate_tree(source, expected)
        run('hdiutil', 'create', '-size', '256m', '-layout', 'NONE', '-fs', 'APFS',
            '-volname', 'OwnerData', '-type', 'UDTO', raw)
        data_device, volume = attach(raw, out / 'data-mount', False)
        try:
            info = plistlib.loads(run('diskutil', 'info', '-plist', volume))
            volume_uuid = info['VolumeUUID']
            copy_tree(source, out / 'data-mount', expected)
            setup = write_setup_preferences(out / 'data-mount')
            validate_tree(out / 'data-mount', expected)
            run('sync')
            run('diskutil', 'unmount', volume)
            with (out / 'fsck-apfs.log').open('wb') as log:
                result = subprocess.run(['/sbin/fsck_apfs', '-n', data_device], stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise ValueError(f'Apple fsck_apfs failed: {result.returncode}')
        finally:
            run('hdiutil', 'detach', data_device)
    finally:
        run('hdiutil', 'detach', os_device)
    if raw.stat().st_size != LOGICAL or raw.open('rb').read(36)[32:36] != b'NXSB':
        raise ValueError('Expected raw APFS container, no GPT wrapper')
    metadata = pack(raw, out / 'data-21H461.dbr')
    if metadata['sha256'] != sha(raw):
        raise ValueError('Packed reconstruction differs from complete raw image')
    report = dict(metadata, volume_uuid=volume_uuid, source_build='21H461',
                  source_os_member=member, source_os_sha256=expected_hash,
                  source_launchd_sha256=LAUNCHD,
                  source_entries=len(expected), source_regular_files=sum(e['type'] == 'file' for e in expected),
                  expected_var_sha256=sha(Path(__file__).resolve().with_name('expected-var.json')),
                  apple_fsck_passed=True, initial_tree_content_and_posix_metadata_checked=True,
                  guest_mutable=True, persistent=False, initial_seed_signature_verified=False,
                  owner_keybag_created=False, os_inputs_modified=False,
                  skip_setup=True, setup_preferences=setup,
                  runtime_qualified=False,
                  limits=['Factory var tree plus two PurpleBuddy preferences; staged_system_apps excluded.',
                          'Mutable RAM Data, discarded on reboot. No user keybag, signed Data policy, or production storage claim.',
                          'Native iPadOS APFS mount, tzinit and later userspace still require device evidence.'])
    (out / 'seed.json').write_text(json.dumps(report, indent=2) + '\n')
    shutil.copyfile(Path(__file__).resolve().with_name('expected-var.json'), out / 'expected-var.json')
    print(json.dumps(report, indent=2), flush=True)
    dmg.unlink()
    raw.unlink()


if __name__ == '__main__':
    main()
