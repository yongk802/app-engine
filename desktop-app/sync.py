#!/usr/bin/env python3
"""Copy/check the standalone desktop sources in the two sibling repositories."""
import argparse
import pathlib
import shutil
import subprocess


def main():
    here = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=pathlib.Path)
    parser.add_argument('--target', type=pathlib.Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source = (args.source or (here if (here / 'engine.py').is_file() else here.parent / 'app-engine')).resolve()
    target = (args.target or (here if (here / 'app_store').is_dir() else here.parent / 'app-store')).resolve()
    if source == target or not (source / 'engine.py').is_file() or not (target / 'app_store' / '__main__.py').is_file():
        parser.error('Supply separate app-engine source and app-store target repository paths.')
    paths = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z', '--cached', '--others', '--exclude-standard', 'desktop-app/']).decode().split('\0')
    changed = []
    for path in sorted(set(filter(None, paths))):
        src, dst = source / path, target / path
        if not src.is_file():
            continue
        if dst.is_file() and src.read_bytes() == dst.read_bytes() and src.stat().st_mode & 0o111 == dst.stat().st_mode & 0o111:
            continue
        changed.append(path)
        if not args.check:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    print(('Different: ' if args.check else 'Synchronized: ') + str(len(changed)) + ' source files')
    if args.check and changed:
        print('\n'.join(changed))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
