#!/usr/bin/env python3
"""Prepare private PGDG binaries on Ubuntu resolute amd64, without sudo or dpkg installation.

Downloads are verified against SHA256 in the HTTPS PGDG package index. Existing
system PostgreSQL and service configuration are never modified. Other operating
systems should provide compatible binaries through PG_AWR_TEST_PREFIX instead.
"""
import gzip
import hashlib
from pathlib import Path
import platform
import shutil
import subprocess
import urllib.request
from cluster import ROOT

if platform.machine() not in ('x86_64','amd64') or 'resolute' not in Path('/etc/os-release').read_text():
    raise SystemExit('This helper requires Ubuntu resolute amd64; provision matching private binaries for your OS.')
base='https://apt.postgresql.org/pub/repos/apt/'
index=gzip.decompress(urllib.request.urlopen(base+'dists/resolute-pgdg/main/binary-amd64/Packages.gz').read()).decode()
target=ROOT/'.local/pg'
cache=ROOT/'.local/packages';cache.mkdir(parents=True,exist_ok=True)
wanted={f'postgresql{suffix}-{major}' for major in (15,16,17,18) for suffix in ('','-client')}
for paragraph in index.split('\n\n'):
    fields=dict(line.split(': ',1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
    name=fields.get('Package')
    if name not in wanted:continue
    wanted.remove(name)
    package=cache/Path(fields['Filename']).name
    if not package.exists() or hashlib.sha256(package.read_bytes()).hexdigest()!=fields['SHA256']:
        urllib.request.urlretrieve(base+fields['Filename'],package)
    if hashlib.sha256(package.read_bytes()).hexdigest()!=fields['SHA256']:
        raise SystemExit('Package hash mismatch: '+name)
    subprocess.run(['dpkg-deb','-x',str(package),str(target)],check=True)
    print(name,fields['Version'],flush=True)
if wanted:raise SystemExit('Packages missing: '+str(wanted))
for major in (15,16,17,18):
    subprocess.run([str(target/f'usr/lib/postgresql/{major}/bin/postgres'),'--version'],check=True)
print('Private binaries ready. Run make, then python3 tests/integration.py --reset.')
