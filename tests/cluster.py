"""Disposable, user-owned local clusters. Never points at an existing cluster."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('pgawr_installer', str(ROOT/'bin/pg-awr-install'))
spec = importlib.util.spec_from_loader(loader.name, loader)
installer = importlib.util.module_from_spec(spec)
loader.exec_module(installer)
DB = installer.DB
Error = installer.Error


class Cluster:
    def __init__(self, version, port=None):
        self.version = str(version)
        self.prefix = Path(os.environ.get('PG_AWR_TEST_PREFIX', ROOT/'.local/pg'))
        self.bindir = self.prefix/f'usr/lib/postgresql/{version}/bin'
        self.base = ROOT/f'.local/test-{version}'
        self.data = self.base/'data'
        self.socket = self.base/'socket'
        self.port = port or 55400 + int(version)
        self.params = dict(host=str(self.socket),port=self.port,user='postgres',dbname='postgres')

    def command(self, name, *args, **kw):
        result=subprocess.run([str(self.bindir/name), *map(str,args)],text=True,capture_output=True,**kw)
        if result.returncode: raise RuntimeError(f'{name} failed: {result.stdout} {result.stderr}')
        return result

    def setup(self, extension_payload=None, default_version=None):
        if not self.bindir.exists():
            raise RuntimeError(f'Missing local PostgreSQL {self.version} binaries: {self.bindir}')
        if self.data.exists():
            raise RuntimeError(f'Refusing to overwrite {self.data}; remove disposable cluster explicitly first.')
        self.socket.mkdir(parents=True,exist_ok=True)
        self.base.chmod(0o700)
        self.socket.chmod(0o700)
        sharedir = self.prefix/f'usr/share/postgresql/{self.version}/extension'
        sources = ([ROOT/'pg_awr.control', *ROOT.glob('sql/pg_awr--*.sql')]
                   if extension_payload is None else
                   [extension_payload/'pg_awr.control', *extension_payload.glob('pg_awr--*.sql')])
        for src in sources:
            shutil.copy2(src,sharedir/src.name)
        if default_version is not None:
            # Historical tests restore dumps using the historical extension default.
            import re
            control = sharedir/'pg_awr.control'
            control.write_text(re.sub(r"default_version = '[^']+'",
                                     "default_version = '"+default_version+"'", control.read_text()))
        self.command('initdb','-D',self.data,'-U','postgres','--auth-local=trust','--auth-host=scram-sha-256','--no-locale','-E','UTF8')
        with (self.data/'postgresql.conf').open('a') as f:
            f.write(f"\nport={self.port}\nunix_socket_directories='{self.socket}'\nlisten_addresses='127.0.0.1'\nshared_preload_libraries='pg_stat_statements'\npg_stat_statements.max=500\ntrack_io_timing=on\ntrack_wal_io_timing=on\nfsync=on\n")
        (self.base/'pg_awr_test_cluster').write_text(self.version)
        self.start()

    def reset(self):
        if not self.base.exists(): return
        marker=self.base/'pg_awr_test_cluster'
        if not marker.is_file() or marker.read_text()!=self.version or self.base.is_symlink():
            raise RuntimeError('Refusing to remove unmarked test cluster '+str(self.base))
        if (self.data/'postmaster.pid').exists(): self.stop()
        shutil.rmtree(self.base)

    def start(self):
        self.command('pg_ctl','-D',self.data,'-l',self.base/'postgres.log','-w','start')

    def stop(self):
        self.command('pg_ctl','-D',self.data,'-m','fast','-w','stop')

    def db(self, database='postgres', user='postgres', password=None, tcp=False):
        return DB(dict(self.params,dbname=database,user=user,**({'password':password} if password else {}),**({'host':'127.0.0.1'} if tcp else {})))

    def install_args(self, *extra):
        return [str(ROOT/'bin/pg-awr-install'),'--host',str(self.socket),'--port',str(self.port),'--username','postgres',*extra]
