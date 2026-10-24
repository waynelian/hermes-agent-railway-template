#!/usr/bin/env python3
"""Fail-closed default-profile admission, before supervisor starts.

Run only with the reviewed image Python. No restoration, deletion, notification,
provider lifecycle, model calls, or dependency repair is performed. The private
one-time checkpoint is deliberately small; it is NOT a full Hermes backup.
Helpers accept fixture paths for offline tests; CLI home selection is not optional.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
SOURCE = Path('/usr/local/lib/hermes-agent')
SOURCE_COMMIT = 'bf0203a794a07aac3ff764972ab9087baa4e4f4e'
PLUGIN_COMMIT = '827c639e3cf38b17e9b5c1b6a4ef2ef7880ae953'
PLUGIN_REPO = 'https://github.com/itpartypattaya/hermes-plugin-holographic'
REQUIRED_EXTRAS = frozenset('all acp computer-use doc-extract google telegram tts-premium vertex vision voice web wecom youtube'.split())
# Full observed CI peak, including workspace/cache, before the safety reserve.
ENVIRONMENT_BYTES = 2395238400
DEFAULT_RESERVE_BYTES = 512 * 1024 * 1024
CHECKPOINT_LIMIT = 64 * 1024 * 1024
CHECKPOINT_NAME = '.holographic-v0225-rollback'
SIDECAR = '.hermes-catalog.json'


class PreparationError(RuntimeError):
    """Messages are static status codes, never configuration or exception values."""


def require(condition, code):
    if not condition:
        raise PreparationError(code)


def plain_path(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path, 'redirected-or-relative-path')
    require(not path.is_symlink(), 'redirected-path')
    return path


def validate_home(home, default_root, active_home, environ):
    """No named-profile selection, even when its spelling redirects to default."""
    home = plain_path(home)
    require(home == plain_path(default_root) == plain_path(active_home), 'default-home-required')
    require('profiles' not in home.parts, 'named-profile-home-forbidden')
    require(environ.get('HERMES_PROFILE', 'default') in ('', 'default'), 'named-profile-selection-forbidden')
    for key in ('HERMES_CONFIG', 'HERMES_ENV', 'HERMES_ENABLE_PROJECT_PLUGINS'):
        require(not environ.get(key), 'alternate-config-or-project-selection-forbidden')
    return home


def read_config(path):
    # Do not load merged config: it expands secrets and can initialize user state.
    import yaml
    with path.open(encoding='utf-8-sig') as stream:
        config = yaml.safe_load(stream)
    require(config is None or isinstance(config, dict), 'config-must-be-mapping')
    return config or {}


def desired_config(config):
    result = copy.deepcopy(config)
    plugins = result.setdefault('plugins', {})
    require(isinstance(plugins, dict), 'plugins-must-be-mapping')
    settings = plugins.setdefault('hermes-memory-store', {})
    require(isinstance(settings, dict), 'provider-settings-must-be-mapping')
    settings.setdefault('mirror_memory_writes', True)
    settings.setdefault('recall_indicator', False)
    disabled = plugins.get('disabled', [])
    require(isinstance(disabled, list), 'disabled-plugins-must-be-list')
    require(not {'holographic', 'hermes-memory-store', PLUGIN_REPO} & set(disabled),
            'configured-provider-explicitly-disabled')
    return result


def database_path(home, config):
    settings = config.get('plugins', {}).get('hermes-memory-store', {})
    raw = settings.get('db_path', '$HERMES_HOME/memory_store.db')
    require(isinstance(raw, str) and raw != ':memory:', 'persistent-database-required')
    # Match the pinned provider's supported expansion, not arbitrary env secrets.
    expanded = raw.replace('${HERMES_HOME}', str(home)).replace('$HERMES_HOME', str(home))
    require('$' not in expanded, 'unsupported-database-variable')
    path = plain_path(Path(expanded).expanduser())
    require(path.is_relative_to(home) and path != home, 'database-outside-default-home')
    rel = path.relative_to(home)
    require(not set(rel.parts) & {'profiles', 'plugins', 'sessions', CHECKPOINT_NAME}, 'unsafe-database-location')
    require(path.name not in ('state.db', 'config.yaml', '.env', 'auth.json'), 'unsafe-database-location')
    require(not path.exists() or path.is_file(), 'database-not-regular-file')
    return path


@contextlib.contextmanager
def readonly_database(path):
    # URI encoding handles spaces, # and ? without opening a different database.
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15)
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def fact_snapshot(path):
    """Private in-memory rows: never serialize facts into logs or worker argv."""
    if not path.exists():
        return None
    with readonly_database(path) as db:
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'database-integrity-failed')
        columns = [row[1] for row in db.execute('PRAGMA table_info(facts)') if row[1] != 'hrr_vector']
        require('fact_id' in columns and 'content' in columns, 'unsupported-fact-schema')
        # Metadata columns are discovered, not limited to the legacy nine fields.
        quoted = ', '.join('"' + col.replace('"', '""') + '"' for col in columns)
        return columns, db.execute(f'SELECT {quoted} FROM facts ORDER BY fact_id').fetchall()


def compare_facts(path, before):
    after = fact_snapshot(path)
    require(after is not None, 'migrated-database-missing')
    if before is None:
        require(not after[1], 'unexpected-fact-writes')
        return
    columns, rows = before
    with readonly_database(path) as db:
        quoted = ', '.join('"' + col.replace('"', '""') + '"' for col in columns)
        require(db.execute(f'SELECT {quoted} FROM facts ORDER BY fact_id').fetchall() == rows,
                'original-facts-or-metadata-changed')


def metadata(home):
    path = plain_path(home / 'plugins/.install-metadata.json')
    data = json.loads(path.read_text()) if path.exists() else {}
    require(isinstance(data, dict), 'invalid-plugin-metadata')
    return data


def private_write(path, data):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def checkpoint(home, db_path):
    """O_EXCL-style one-time snapshot; incomplete/tampered snapshots block startup."""
    target = plain_path(home / CHECKPOINT_NAME)
    if target.exists():
        require(stat.S_IMODE(target.stat().st_mode) == 0o700, 'checkpoint-directory-not-private')
        manifest_path = plain_path(target / 'manifest.json')
        require(manifest_path.is_file(), 'incomplete-checkpoint-manual-review-required')
        require(stat.S_IMODE(manifest_path.stat().st_mode) == 0o600, 'checkpoint-file-not-private')
        manifest = json.loads(manifest_path.read_text())
        require(manifest.get('source') == SOURCE_COMMIT and manifest.get('database') == str(db_path),
                'checkpoint-identity-mismatch')
        files = manifest.get('files')
        require(isinstance(files, dict) and 'config.yaml' in files, 'invalid-checkpoint')
        require(set(files) <= {'config.yaml', 'memory_store.db', 'install-metadata.json', 'catalog.json'},
                'invalid-checkpoint')
        for name, digest in files.items():
            path = plain_path(target / name)
            require(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600, 'checkpoint-file-not-private')
            require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'checkpoint-content-mismatch')
        return target
    inputs = {'config.yaml': home / 'config.yaml',
              'install-metadata.json': home / 'plugins/.install-metadata.json',
              'catalog.json': home / 'plugins/holographic' / SIDECAR}
    for path in inputs.values():
        plain_path(path)
    size = sum(path.stat().st_size for path in inputs.values() if path.exists())
    if db_path.exists():
        with readonly_database(db_path) as db:
            size += db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
    require(size <= CHECKPOINT_LIMIT, 'checkpoint-too-large-manual-backup-required')
    require(shutil.disk_usage(home).free >= size + DEFAULT_RESERVE_BYTES, 'insufficient-checkpoint-headroom')
    target.mkdir(mode=0o700)
    for name, path in inputs.items():
        if path.exists():
            private_write(target / name, path.read_bytes())
    if db_path.exists():
        backup = target / 'memory_store.db'
        private_write(backup, b'')
        # Same sqlite3 module/interpreter as the pinned application's MemoryStore.
        with readonly_database(db_path) as source:
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
                require(destination.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'backup-integrity-failed')
            finally:
                destination.close()
        os.chmod(backup, 0o600)
        fsync_directory(backup)
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in target.iterdir()}
    private_write(target / 'manifest.json', json.dumps({'source': SOURCE_COMMIT, 'database': str(db_path),
                  'database_existed': db_path.exists(), 'files': files}, sort_keys=True).encode())
    fsync_directory(target)
    fsync_directory(home)
    return target


class Runtime:
    """Small lazy adapter around the reviewed Hermes/PM APIs; mockable offline."""

    def __init__(self, home, source):
        self.home, self.source = home, source

    def entry(self):
        from hermes_cli.plugin_catalog import get_catalog_dir, get_catalog_entry
        directory = get_catalog_dir()
        require(directory.resolve().is_relative_to(self.source), 'in-tree-catalog-required')
        entry = get_catalog_entry('holographic', catalog_dir=directory)
        require(entry is not None and entry.sha == PLUGIN_COMMIT and entry.repo == PLUGIN_REPO
                and entry.name == 'holographic', 'catalog-pin-mismatch')
        return entry

    def current(self):
        from pm.client import venv_is_current
        return venv_is_current(project_root=self.source)

    def extras(self):
        from pm.environments import runtime_facts_path
        from pm.features import read_features
        from pm.lock import Facts
        from pm.paths import facts_path
        # Match PM's own precedence: a fresh mounted home has no runtime
        # ledger, but --record-selection baked the exact baseline in the image.
        record = Facts(runtime_facts_path(self.source), strict=True).get('venv')
        if record is None:
            record = Facts(facts_path(), strict=True).get('venv')
        values = record.get('extras') if record is not None else None
        if values is None:
            values = read_features()
        require(isinstance(values, list) and all(isinstance(item, str) for item in values), 'extras-record-missing')
        require(REQUIRED_EXTRAS <= set(values), 'baked-extras-not-preserved')
        return set(values)

    def verify_install(self, entry):
        from hermes_cli.plugins_cmd_catalog import catalog_install_record
        from plugins.memory import find_provider_dir
        target = plain_path(self.home / 'plugins/holographic')
        require(target.is_dir(), 'installed-plugin-missing')
        record = metadata(self.home).get('holographic', {})
        require(record.get('pinned') is True and record.get('revision') == entry.sha
                and record.get('source') == entry.repo, 'installed-plugin-provenance-mismatch')
        expected = {'name': entry.name, 'repo': entry.repo, 'tier': entry.tier, 'pin': entry.sha, 'sha': entry.sha}
        require(all(record.get('catalog', {}).get(k) == v for k, v in expected.items()), 'installed-catalog-mismatch')
        sidecar = catalog_install_record(target)
        sidecar_expected = {('catalog_name' if k == 'name' else k): v for k, v in expected.items()}
        require(sidecar is not None and all(sidecar.get(k) == v for k, v in sidecar_expected.items()),
                'catalog-sidecar-mismatch')
        for args, expected_output in ((['rev-parse', 'HEAD'], entry.sha), (['status', '--porcelain', '--untracked-files=no'], '')):
            proc = subprocess.run(['git', '-C', str(target), *args], capture_output=True, text=True, timeout=30)
            require(proc.returncode == 0 and proc.stdout.strip() == expected_output, 'plugin-git-revision-or-tree-mismatch')
        found = find_provider_dir('holographic')
        require(found is not None and found.resolve() == target, 'provider-resolution-mismatch')

    def install(self, entry):
        from hermes_cli.plugins_cmd_catalog import install_catalog_entry
        target, manifest, name = install_catalog_entry(entry, force=False, ref=entry.sha,
                                                     assume_deps_consent=True, python_deps=True)
        require(Path(target) == self.home / 'plugins/holographic' and name == 'holographic'
                and manifest.get('name') == 'holographic', 'installer-returned-wrong-provider')

    def write_config(self, config):
        from hermes_cli.config import atomic_config_write
        atomic_config_write(self.home / 'config.yaml', config)

    def migrate(self):
        from pm.environments import selected_venv, venv_command
        command = [*venv_command(self.source, selected_venv(self.source), options=['-B']),
                   str(Path(__file__).resolve()), '--worker']
        # No credentials or private config in argv/stdin. Never relay child output.
        proc = subprocess.run(command, cwd=self.source, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=300)
        require(proc.returncode == 0, 'offline-migration-worker-failed')


def prepare(home, source, runtime=None, reserve_bytes=DEFAULT_RESERVE_BYTES):
    """Explicit paths are a test seam, NOT a CLI override of default-home checks."""
    home, source = plain_path(home), plain_path(source)
    config_path = plain_path(home / 'config.yaml')
    if not config_path.exists():
        return 'no-config'
    config = read_config(config_path)
    memory = config.get('memory', {})
    require(isinstance(memory, dict), 'memory-must-be-mapping')
    if memory.get('provider') != 'holographic':
        return 'other-provider'
    stamp = json.loads((source / 'install-stamp.json').read_text())
    require(stamp.get('displayVersion') == '0.22.5' and stamp.get('commit') == SOURCE_COMMIT,
            'reviewed-source-version-required')
    require(isinstance(reserve_bytes, int) and reserve_bytes >= DEFAULT_RESERVE_BYTES, 'reserve-below-safe-minimum')
    proposed = desired_config(config)
    db_path = database_path(home, proposed)
    runtime = runtime or Runtime(home, source)
    # Directory flock creates no state and prevents two bootstrap instances racing.
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(read_config(config_path) == config, 'config-changed-before-lock')
        original_bytes = config_path.read_bytes()
        before = fact_snapshot(db_path)
        original_metadata = metadata(home)
        entry = runtime.entry()
        installed = (home / 'plugins/holographic').exists()
        require(installed or 'holographic' not in original_metadata, 'orphaned-plugin-record-manual-review-required')
        plain_path(home / 'plugins/holographic')
        if installed:
            runtime.verify_install(entry)
            require(runtime.current(), 'installed-dependency-environment-stale-manual-review-required')
        extras_before = runtime.extras()
        checkpoint(home, db_path)
        if not installed:
            # Admission publishes a whole environment even when wheels are baked.
            require(shutil.disk_usage(home).free >= ENVIRONMENT_BYTES + reserve_bytes,
                    'insufficient-environment-headroom')
            runtime.install(entry)
        runtime.verify_install(entry)
        require(runtime.current(), 'dependency-environment-not-current')
        require(extras_before <= runtime.extras(), 'existing-extras-lost')
        require(config_path.read_bytes() == original_bytes, 'installer-changed-config')
        require({k: v for k, v in metadata(home).items() if k != 'holographic'} ==
                {k: v for k, v in original_metadata.items() if k != 'holographic'}, 'unrelated-plugin-metadata-changed')
        admitted_metadata = metadata(home)
        if installed:
            require(admitted_metadata == original_metadata, 'existing-plugin-metadata-changed')
        if db_path.exists():
            compare_facts(db_path, before)
        else:
            require(before is None, 'original-database-missing')
        if proposed != config:
            runtime.write_config(proposed)
        require(read_config(config_path) == proposed, 'config-readback-mismatch')
        runtime.migrate()
        compare_facts(db_path, before)
        require(read_config(config_path) == proposed, 'migration-changed-config')
        runtime.verify_install(entry)
        require(runtime.current() and extras_before <= runtime.extras(), 'final-dependency-readback-failed')
        require(metadata(home) == admitted_metadata, 'final-plugin-metadata-changed')
        return 'verified-existing' if installed else 'installed-and-verified'
    finally:
        os.close(fd)


def offline_guard(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind',
                 'socket.sendto', 'socket.sendmsg', 'http.client.connect', 'urllib.Request',
                 'subprocess.Popen', 'os.system'):
        raise PreparationError('offline-worker-forbids-network-and-processes')


def migration_worker(home, source):
    from pm.environments import running_from_selected_environment
    runtime = Runtime(home, source)
    require(running_from_selected_environment(source) and runtime.current(), 'worker-selected-environment-required')
    runtime.extras()
    from pm.extras import available
    require(all(available(extra) for extra in REQUIRED_EXTRAS - {'all'}), 'worker-extra-import-missing')
    config = read_config(home / 'config.yaml')
    db_path = database_path(home, config)
    before = fact_snapshot(db_path)
    settings = config.get('plugins', {}).get('hermes-memory-store', {})
    sys.addaudithook(offline_guard)
    # This imports the real package under the core-owned provider namespace; it
    # does NOT construct/initialize/register a provider or an embedding client.
    from plugins.memory import import_provider_module
    module = import_provider_module('holographic', 'store')
    require(Path(module.__file__).resolve().parent == home / 'plugins/holographic', 'worker-store-origin-mismatch')
    require(module.sqlite3.sqlite_version == sqlite3.sqlite_version, 'application-sqlite-mismatch')
    store = module.MemoryStore(db_path=db_path, default_trust=float(settings.get('default_trust', 0.5)),
                               hrr_dim=int(settings.get('hrr_dim', 1024)))
    try:
        version = store.read("SELECT value FROM meta WHERE key='schema_version'")
        require(len(version) == 1 and str(version[0][0]) == str(module.SCHEMA_VERSION), 'migration-schema-version-mismatch')
    finally:
        store.close()
    compare_facts(db_path, before)


@contextlib.contextmanager
def quiet_output():
    """Suppress dependency logs at fd level: they may contain private config."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    with open(os.devnull, 'w') as sink:
        try:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, copy_fd in zip((1, 2), saved):
                os.dup2(copy_fd, fd)
                os.close(copy_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    result = 'failed'
    try:
        with quiet_output():
            sys.path.insert(0, str(SOURCE))
            from hermes_constants import get_default_hermes_root, get_hermes_home
            home = validate_home(Path(os.environ.get('HERMES_HOME') or get_hermes_home()),
                                 get_default_hermes_root(), get_hermes_home(), os.environ)
            if args.worker:
                stamp = json.loads((SOURCE / 'install-stamp.json').read_text())
                require(stamp.get('commit') == SOURCE_COMMIT and stamp.get('displayVersion') == '0.22.5',
                        'reviewed-source-version-required')
                migration_worker(home, SOURCE)
                result = 'worker-verified'
            else:
                reserve = int(os.environ.get('HERMES_HOLOGRAPHIC_RESERVE_BYTES', DEFAULT_RESERVE_BYTES))
                result = prepare(home, SOURCE, reserve_bytes=reserve)
    except PreparationError as exc:
        print('HOLOGRAPHIC_PREPARE=failed:' + str(exc), file=sys.stderr)
        return 1
    except BaseException:
        # Never print raw exceptions/tracebacks: YAML/parser/installer errors can
        # include config values. The private checkpoint is retained for review.
        print('HOLOGRAPHIC_PREPARE=failed:unexpected-error-manual-review-required', file=sys.stderr)
        return 1
    print('HOLOGRAPHIC_PREPARE=' + result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
