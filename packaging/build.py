#!/usr/bin/env python3
"""Build native, architecture-independent packages without a running server.

Debian/Ubuntu use versioned /usr/share/postgresql paths. RPMs target the
versioned PostgreSQL Global Development Group (PGDG) packages on RHEL-family
systems. Use --pg-major to choose the server ABI/layout, not the build host's.
"""
import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r"default_version\s*=\s*'([^']+)'", (ROOT / 'pg_awr.control').read_text())[1]


def run(*args, **kwargs):
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def source_tree(destination):
    """Allowlist release files; never include local clusters or credentials."""
    for name in ('Makefile', 'pg_awr.control', 'README.md', 'pg_awr_technical_spec.md'):
        shutil.copy2(ROOT / name, destination / name)
    for name, pattern in (('sql', '*.sql'), ('bin', 'pg-awr-*'),
                          ('docs', '*.md'), ('packaging', '*'), ('tests', '*.py')):
        (destination / name).mkdir()
        for path in sorted((ROOT / name).glob(pattern)):
            if path.is_file():
                shutil.copy2(path, destination / name / path.name)
    shutil.copytree(ROOT / 'examples', destination / 'examples')
    for path in sorted((ROOT / 'docs').glob('*.json')):
        shutil.copy2(path, destination / 'docs' / path.name)
    (destination / 'sql/pg_awr--1.1.sql').write_bytes(
        (ROOT / 'sql/pg_awr--1.0.sql').read_bytes() +
        (ROOT / 'sql/pg_awr--1.0--1.1.sql').read_bytes())


def copy_file(source, target, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(mode)


def deb(source, work, output, major, release):
    name = f'postgresql-{major}-pg-awr'
    stage = work / 'deb'
    for path in [source / 'pg_awr.control', *sorted((source / 'sql').glob('pg_awr--*.sql'))]:
        copy_file(path, stage / f'usr/share/postgresql/{major}/extension' / path.name)
    copy_file(source / 'bin/pg-awr-job', stage / f'usr/lib/postgresql/{major}/bin/pg-awr-job', 0o755)
    doc = stage / f'usr/share/doc/{name}'
    copy_file(source / 'README.md', doc / 'README.md')
    copy_file(source / 'packaging/copyright', doc / 'copyright')
    shutil.copytree(source / 'docs', doc / 'docs')
    shutil.copytree(source / 'examples', doc / 'examples')
    for path in doc.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    size = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file())
    control = stage / 'DEBIAN'
    control.mkdir()
    (control / 'control').write_text(f'''Package: {name}
Version: {VERSION}-{release}
Section: database
Priority: optional
Architecture: all
Maintainer: PostAWR maintainers <postawr@localhost>
Depends: postgresql-{major}, postgresql-client-{major}
Installed-Size: {(size + 1023) // 1024}
Description: Historical PostgreSQL performance statistics and interval reports
 SQL/PL/pgSQL extension with snapshots, activity samples and JSON/HTML reports.
 Enable with CREATE EXTENSION pg_awr CASCADE after preloading pg_stat_statements.
 No bootstrap installer, dedicated database, Python runtime or compiler needed.
''')
    (control / 'md5sums').write_text(''.join(
        f'{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(stage)}\n'
        for path in sorted((stage / 'usr').rglob('*')) if path.is_file()))
    target = output / f'{name}_{VERSION}-{release}_all.deb'
    run('dpkg-deb', '--root-owner-group', '--build', stage, target)
    return [target]


def rpm(source, work, output, major, release):
    top = work / 'rpmbuild'
    for name in ('BUILD', 'BUILDROOT', 'RPMS', 'SOURCES', 'SPECS', 'SRPMS'):
        (top / name).mkdir(parents=True)
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    archive = top / f'SOURCES/pg_awr-{VERSION}.tar.gz'
    with archive.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=epoch) as gz:
        with tarfile.open(fileobj=gz, mode='w') as tar:
            for path in [source, *sorted(source.rglob('*'))]:
                info = tar.gettarinfo(str(path), str(Path(f'pg_awr-{VERSION}') / path.relative_to(source)))
                info.uid = info.gid = 0
                info.uname = info.gname = 'root'
                info.mtime = epoch
                if path.is_file():
                    with path.open('rb') as data:
                        tar.addfile(info, data)
                else:
                    tar.addfile(info)
    run('rpmbuild', '-ba', '--define', f'_topdir {top}', '--define', f'_dbpath {top / "rpmdb"}',
        '--define', f'pgmajor {major}', '--define', f'pgawr_version {VERSION}',
        '--define', f'pgawr_release {release}', source / 'packaging/pg_awr.spec')
    artifacts = [archive, *sorted((top / 'RPMS').rglob('*.rpm')), *sorted((top / 'SRPMS').glob('*.rpm'))]
    for path in artifacts:
        shutil.copy2(path, output / path.name)
    return [output / path.name for path in artifacts]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('format', choices=('deb', 'rpm', 'all'))
    parser.add_argument('--pg-major', type=int, choices=range(15, 19), default=18)
    parser.add_argument('--release', default='1')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    if not re.fullmatch(r'[1-9][0-9]*', args.release):
        parser.error('--release must be a positive integer')
    formats = ('deb', 'rpm') if args.format == 'all' else (args.format,)
    for fmt in formats:
        tool = {'deb': 'dpkg-deb', 'rpm': 'rpmbuild'}[fmt]
        if not shutil.which(tool):
            parser.error(f'{tool} is required to build {fmt} packages')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    with tempfile.TemporaryDirectory(prefix='pg-awr-package-') as tmp:
        work = Path(tmp)
        source = work / f'pg_awr-{VERSION}'
        source.mkdir()
        source_tree(source)
        for fmt in formats:
            artifacts.extend({'deb': deb, 'rpm': rpm}[fmt](source, work, output, args.pg_major, args.release))
    for path in artifacts:
        print(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path}')


if __name__ == '__main__':
    main()
