#!/usr/bin/env python3
"""Real, synthetic-only holographic migration/admission probe for the candidate image.

Run with the image's .venv/bin/python, not the Hermes/gateway entrypoint::

  docker run --rm --tmpfs /data:rw,exec,size=8g \
    -e HERMES_HOME=/data/ci-holographic/.hermes \
    -v "$PWD/verify_holographic_migration.py:/probe.py:ro" \
    --entrypoint /usr/local/lib/hermes-agent/.venv/bin/python IMAGE /probe.py

/data must be a fresh tmpfs; the fixture is deliberately retained for inspection.
The installer needs public GitHub/PyPI access. No credentials, model calls, gateway,
notification transports, production files, or production database are used. The
second admission/current-environment check is a no-force, no-reinstall operation.
A final HOLOGRAPHIC_MIGRATION_REPORT= JSON line is emitted on success AND failure.
Peak allocation is sampled, not an exact kernel high-water mark (interval 0.2s).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

sys.dont_write_bytecode = True
DATA = Path('/data')
FIXTURE = DATA / 'ci-holographic'
HOME = FIXTURE / '.hermes'
SOURCE = Path('/usr/local/lib/hermes-agent')
EXPECTED_SOURCE_REVISION = 'bf0203a794a07aac3ff764972ab9087baa4e4f4e'
EXPECTED_PLUGIN_REVISION = '827c639e3cf38b17e9b5c1b6a4ef2ef7880ae953'
LEGACY_FACT = 'The synthetic amber observatory keeps a brass telescope.'
NEW_FACT = 'The synthetic violet workshop keeps a ceramic compass.'
LEGACY_ROW = (41, LEGACY_FACT, 'project', 'synthetic,legacy', 0.73, 7, 3,
              '2026-01-02 03:04:05', '2026-02-03 04:05:06')

# Verbatim ast.literal_eval of the top-level _SCHEMA assignment in the PARENT
# /usr/local/lib/hermes-agent/plugins/memory/holographic/store.py. Never import
# that production module or open its database; this literal is the old schema.
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
LEGACY_COLUMNS = ('fact_id, content, category, tags, trust_score, retrieval_count, '
                  'helpful_count, created_at, updated_at')
PLUGIN_SETTINGS = {
    'embeddings': 'off', 'auto_extract': False, 'mirror_memory_writes': True,
    'recall_indicator': False, 'scope': 'profile', 'cron_writes': False,
    'db_path': '$HERMES_HOME/memory_store.db',
}
CONFIG = {
    'memory': {'provider': 'holographic'},
    'plugins': {
        'enabled': ['disk-cleanup', 'openai-codex', 'url-reputation'],
        'disabled': [], 'hermes-memory-store': PLUGIN_SETTINGS,
        'ci-sentinel': {'keep': ['synthetic', 17], 'enabled': False},
    },
    'ci_sentinel': {'keep': 'unrelated configuration must survive'},
}
METADATA_SENTINEL = {
    'source': 'https://example.invalid/ci-synthetic-sentinel',
    'revision': '1' * 40, 'pinned': True,
    'ci_sentinel': {'keep': 'unrelated plugin metadata must survive'},
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def mounts():
    """Parse Linux mountinfo without shell commands or state-changing imports."""
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), value)
    result = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, fs = left.split(), right.split()
        result.append((Path(unescape(fields[4])), fs[0], fields[5].split(',')))
    return result


def safety_gate():
    """Must run before mkdir, DB access, or any Hermes/third-party imports."""
    require(os.environ.get('HERMES_HOME') == str(HOME),
            f'HERMES_HOME must be exactly {HOME}')
    require(DATA.is_dir() and DATA.resolve() == DATA, '/data must be a real directory')
    records = mounts()
    data_mounts = [row for row in records if row[0] == DATA]
    require(bool(data_mounts) and data_mounts[-1][1] == 'tmpfs',
            'Refusing writes: /data is not an explicit tmpfs mount')
    require('rw' in data_mounts[-1][2], '/data tmpfs is not writable')
    require(not any(path != DATA and path.is_relative_to(DATA) for path, _, _ in records),
            'Refusing nested mounts beneath /data (possible production bind mount)')
    for path in (FIXTURE, HOME):
        require(not path.is_symlink() and path.resolve() == path,
                f'Refusing redirected fixture path: {path}')
    require(not (DATA / '.hermes').exists() and not (DATA / '.hermes').is_symlink(),
            'Refusing /data containing a default/production Hermes home')
    # Do not print values, even when refusing unexpectedly injected credentials.
    secret_names = [key for key, value in os.environ.items() if value and
                    re.search(r'(?:^|_)(?:API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIALS?)$', key)]
    require(not secret_names, 'Refusing credential-bearing environment variables')
    require(not os.environ.get('HERMES_PROFILE'), 'HERMES_PROFILE must be unset')


def allocation():
    info = os.statvfs(DATA)
    return {'used_bytes': (info.f_blocks - info.f_bfree) * info.f_frsize,
            'available_bytes': info.f_bavail * info.f_frsize,
            'capacity_bytes': info.f_blocks * info.f_frsize}


class PeakMonitor:
    def __init__(self):
        self.stop = threading.Event()
        self.peak = allocation()['used_bytes']
        self.samples = 0
        self.error = None
        self.thread = threading.Thread(target=self.watch, name='ci-data-allocation', daemon=True)

    def sample(self):
        self.peak = max(self.peak, allocation()['used_bytes'])
        self.samples += 1

    def watch(self):
        try:
            while not self.stop.wait(0.2):
                self.sample()
        except BaseException as exc:
            self.error = str(exc)

    def finish(self):
        self.stop.set()
        self.thread.join()
        self.sample()
        require(self.error is None, f'Peak allocation monitor failed: {self.error}')


def configure_environment():
    # Retain only non-secret OS basics. In particular drop inherited PM selection,
    # credential paths, project-plugin flags, proxy credentials, and Python paths.
    kept = {key: value for key, value in os.environ.items()
            if key in ('PATH', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR')}
    os.environ.clear()
    os.environ.update(kept)
    os.environ.update({
        'HOME': str(FIXTURE), 'HERMES_HOME': str(HOME),
        'TMPDIR': str(FIXTURE / 'tmp'), 'XDG_CACHE_HOME': str(FIXTURE / 'cache'),
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
        'HERMES_RUNTIME_DIR': str(SOURCE / 'tools'),
        'HERMES_PYTHON': str(SOURCE / '.venv/bin/python'),
        'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': '/dev/null', 'HF_HUB_OFFLINE': '1',
    })
    sys.path.insert(0, str(SOURCE))
    os.chdir(SOURCE)


def seed_fixture():
    # A fresh mount/fixture is required; idempotence is exercised within the run.
    require(not FIXTURE.exists(), 'Fixture already exists; use a fresh /data tmpfs')
    (HOME / 'plugins').mkdir(parents=True)
    (FIXTURE / 'tmp').mkdir()
    (FIXTURE / 'cache').mkdir()
    (HOME / 'config.yaml').write_text(json.dumps(CONFIG, indent=2) + '\n')
    (HOME / 'plugins/.install-metadata.json').write_text(
        json.dumps({'ci-sentinel': METADATA_SENTINEL}, indent=2) + '\n')
    sibling = HOME / 'profiles/ci-sibling'
    sibling.mkdir(parents=True)
    (sibling / 'config.yaml').write_text(json.dumps({
        'memory': {'provider': 'none'}, 'plugins': {'enabled': [], 'disabled': []},
        'ci_sentinel': 'synthetic sibling configuration must remain byte-identical',
    }, indent=2) + '\n')
    with sqlite3.connect(HOME / 'memory_store.db') as db:
        db.executescript(LEGACY_SCHEMA)
        db.execute(f'INSERT INTO facts ({LEGACY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', LEGACY_ROW)
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Legacy integrity failure')
        require(db.execute("SELECT name FROM sqlite_master WHERE name='meta'").fetchone() is None,
                'Legacy schema unexpectedly has new metadata')
    return {path: path.read_bytes() for path in (HOME / 'config.yaml', sibling / 'config.yaml')}


def check_fixtures(snapshots):
    for path, expected in snapshots.items():
        require(path.read_bytes() == expected, f'Sentinel config changed: {path}')
    metadata = json.loads((HOME / 'plugins/.install-metadata.json').read_text())
    require(metadata.get('ci-sentinel') == METADATA_SENTINEL, 'Sentinel plugin metadata changed')


def catalog_entry():
    from hermes_cli.plugin_catalog import get_catalog_dir, get_catalog_entry
    directory = get_catalog_dir()
    require(directory.resolve().is_relative_to(SOURCE), 'Catalog must come from the image source')
    entry = get_catalog_entry('holographic', catalog_dir=directory)
    require(entry is not None, 'Pinned in-tree holographic catalog entry missing')
    require(re.fullmatch(r'[0-9a-f]{40}', entry.sha) is not None, 'Catalog pin is not an exact SHA')
    require(entry.sha == EXPECTED_PLUGIN_REVISION, 'Unexpected catalog revision; review probe APIs before updating')
    return entry


def check_install(entry):
    from hermes_cli.plugins_cmd_catalog import catalog_install_record
    from plugins.memory import find_provider_dir
    target = HOME / 'plugins/holographic'
    require(target.is_dir() and not target.is_symlink(), 'Installed plugin directory missing/redirected')
    metadata = json.loads((HOME / 'plugins/.install-metadata.json').read_text())
    record = metadata.get('holographic', {})
    require(record.get('pinned') is True and record.get('revision') == entry.sha,
            'Installer revision/pin mismatch')
    require(record.get('source') == entry.repo, 'Installer source mismatch')
    catalog = record.get('catalog', {})
    for key, value in {'name': entry.name, 'repo': entry.repo, 'tier': entry.tier,
                       'pin': entry.sha, 'sha': entry.sha}.items():
        require(catalog.get(key) == value, f'Installer catalog {key} mismatch')
    sidecar = catalog_install_record(target)
    require(sidecar is not None and sidecar.get('sha') == entry.sha and sidecar.get('pin') == entry.sha,
            'Catalog sidecar pin mismatch')
    git = subprocess.run(['git', '-C', str(target), 'rev-parse', 'HEAD'],
                         text=True, capture_output=True, check=True, timeout=30)
    require(git.stdout.strip() == entry.sha, 'Actual git checkout does not match catalog pin')
    found = find_provider_dir('holographic')
    require(found is not None and found.resolve() == target, 'Provider did not resolve to installed fixture plugin')
    return {'path': str(target), 'revision': entry.sha, 'pinned': True,
            'catalog': catalog, 'sidecar': sidecar, 'provider_resolution': str(found)}


def ensure_installed(entry):
    """Idempotent wrapper; never force-reinstall an existing provider."""
    from hermes_cli.plugins_cmd_catalog import install_catalog_entry
    from pm.client import venv_is_current
    if (HOME / 'plugins/holographic').exists():
        check_install(entry)
        require(venv_is_current(project_root=SOURCE), 'Existing plugin dependency environment is stale')
        return False
    target, manifest, name = install_catalog_entry(
        entry, force=False, ref=entry.sha, assume_deps_consent=True, python_deps=True)
    require(Path(target) == HOME / 'plugins/holographic' and name == 'holographic',
            'Unexpected installed provider identity')
    require(manifest.get('name') == 'holographic', 'Unexpected plugin manifest')
    return True


def allocated_tree(path):
    """du-like allocated bytes, no symlink traversal, deduplicated hardlinks."""
    seen = set()
    allocated = apparent = count = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for item in [Path(root), *(Path(root) / name for name in files),
                     *(Path(root) / name for name in dirs if (Path(root) / name).is_symlink())]:
            info = item.lstat()
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity)
                allocated += info.st_blocks * 512
                apparent += info.st_size
                count += 1
    return {'allocated_bytes': allocated, 'apparent_bytes': apparent, 'inodes': count,
            'hardlinks_deduplicated': True, 'symlinks_followed': False}


def worker_network_guard(event, args):
    # Network is necessary for installation, NEVER for fact/provider verification.
    # This is an audit refusal, not a mock of the provider, SQLite or PM APIs.
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'http.client.connect', 'urllib.Request', 'subprocess.Popen', 'os.system'):
        raise RuntimeError(f'Offline fact worker forbids {event}')


def check_legacy_row(store):
    rows = store.read(f'SELECT {LEGACY_COLUMNS} FROM facts WHERE fact_id = ?', (LEGACY_ROW[0],))
    require(len(rows) == 1 and tuple(rows[0]) == LEGACY_ROW, 'Legacy fact/metadata not preserved exactly')


def worker(mode):
    from pm.client import venv_is_current
    from pm.environments import running_from_selected_environment, selected_venv
    from plugins.memory import load_memory_provider
    require(running_from_selected_environment(SOURCE), 'Worker is not using PM-selected site-packages')
    require(venv_is_current(project_root=SOURCE), 'Worker dependency environment is not current')
    selected = selected_venv(SOURCE)
    sys.addaudithook(worker_network_guard)
    provider = load_memory_provider('holographic', register_skills=False)
    require(provider is not None, 'Real provider loader failed')
    origin = Path(inspect.getfile(type(provider))).resolve()
    require(origin.parent == HOME / 'plugins/holographic', 'Loaded provider code came from wrong tree')
    store_module = importlib.import_module(type(provider).__module__ + '.store')
    require(store_module.__file__ is not None, 'Store module has no source file')
    require(Path(store_module.__file__).resolve().parent == origin.parent, 'Wrong store module origin')
    require(provider._config == PLUGIN_SETTINGS, 'Provider did not consume exact offline fixture settings')
    require(provider._embedding is None and provider._embedder is None, 'Embeddings unexpectedly enabled')
    require(provider._recall_indicator is False and provider._extracting() is False,
            'Recall indicator/auto-extraction unexpectedly enabled')
    try:
        provider.initialize(session_id='ci-synthetic-migration', hermes_home=str(HOME),
                            agent_context='primary' if mode == 'migrate' else 'subagent')
        store = provider._store
        require(isinstance(store, store_module.MemoryStore), 'Provider failed to initialize the real store')
        require(store.db_path.resolve() == HOME / 'memory_store.db', 'Provider opened wrong database')
        check_legacy_row(store)
        version = store.read("SELECT value FROM meta WHERE key='schema_version'")
        require(len(version) == 1 and str(version[0][0]) == str(store_module.SCHEMA_VERSION),
                'Legacy store did not migrate to the pinned schema version')
        require(store.read('SELECT 1 FROM fact_versions WHERE fact_id = ?', (LEGACY_ROW[0],)),
                'Legacy fact history was not migrated')
        if mode == 'migrate':
            added = json.loads(provider.handle_tool_call('fact_store', {
                'action': 'add', 'content': NEW_FACT, 'category': 'project', 'origin': 'said',
                'tags': 'synthetic,ci',
            }))
            require(not added.get('error') and added.get('fact_id'), f'Real fact add failed: {added}')
            # Exercise the real read-only provider contract for retrieval. Primary
            # search deliberately increments retrieval_count at shutdown; switching
            # context through the public lifecycle avoids changing the legacy
            # sentinel while proving every original metadata value survives.
            provider.shutdown()
            provider.initialize(session_id='ci-synthetic-read', hermes_home=str(HOME), agent_context='subagent')
            store = provider._store
            require(isinstance(store, store_module.MemoryStore), 'Read-only provider re-open failed')
        for query, fact in [('amber observatory', LEGACY_FACT), ('violet workshop', NEW_FACT)]:
            found = json.loads(provider.handle_tool_call('fact_store', {'action': 'search', 'query': query}))
            require(not isinstance(found, dict) or not found.get('error'), f'Real retrieval failed: {found}')
            require(fact in json.dumps(found), f'Real retrieval missed synthetic fact: {query}')
        require(store.read('PRAGMA integrity_check')[0][0] == 'ok', 'Migrated integrity_check failed')
        require(store.read('SELECT COUNT(*) FROM facts')[0][0] == 2, 'Unexpected synthetic fact count')
        check_legacy_row(store)
        # Both independent workers run this; the read worker verifies a true process re-open.
        result = {'mode': mode, 'selected_venv': str(selected), 'packages_current': True,
                  'provider_file': str(origin), 'store_file': str(store_module.__file__),
                  'schema_version': store_module.SCHEMA_VERSION, 'legacy_fact_preserved': True,
                  'legacy_metadata_preserved': True, 'synthetic_fact_retrieved': True,
                  'fact_count': 2, 'integrity_check': 'ok',
                  'packages': {name: importlib.metadata.version(name) for name in ('numpy', 'snowballstemmer')}}
    finally:
        provider.shutdown()
    # Store-level API re-open after provider shutdown, with the same selected interpreter.
    reopened = store_module.MemoryStore(db_path=HOME / 'memory_store.db')
    try:
        check_legacy_row(reopened)
        require(reopened.read('SELECT content FROM facts WHERE content = ?', (NEW_FACT,)),
                'Store re-open lost new fact')
        require(reopened.read('PRAGMA integrity_check')[0][0] == 'ok', 'Reopened integrity_check failed')
    finally:
        reopened.close()
    result['store_reopen_read'] = True
    return result


def run_worker(mode, selected):
    from pm.environments import venv_command
    # -B must be an interpreter option, before the sealed payload's entry script.
    command = [*venv_command(SOURCE, selected, options=['-B']), str(Path(__file__).resolve()), '--worker', mode]
    process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=None, timeout=300)
    require(process.returncode == 0, f'{mode} worker failed with exit {process.returncode}')
    return json.loads(process.stdout)


def integration(report):
    from pm.client import sync_venv, venv_is_current
    from pm.environments import dependency_home_root, runtime_facts_path, selected_venv
    from pm.plugins_state import dependency_homes
    stamp = json.loads((SOURCE / 'install-stamp.json').read_text())
    require(stamp.get('displayVersion') == '0.22.5' and stamp.get('commit') == EXPECTED_SOURCE_REVISION,
            'Probe requires exact reviewed v0.22.5 image source')
    require(dependency_home_root() == HOME, 'PM state escaped dedicated fixture home')
    snapshots = seed_fixture()
    require(set(dependency_homes()) == {HOME, HOME / 'profiles/ci-sibling'},
            'PM did not enumerate exactly the synthetic default/named profiles')
    entry = catalog_entry()
    baseline_venv = selected_venv(SOURCE)
    report['baseline_venv'] = str(baseline_venv)
    require(ensure_installed(entry), 'Fresh fixture did not perform real catalog admission')
    report['install'] = check_install(entry)
    selected = selected_venv(SOURCE)
    require(selected != baseline_venv and selected.resolve().is_relative_to(HOME),
            'Real PM admission did not publish a new fixture dependency generation')
    require(venv_is_current(project_root=SOURCE), 'PM selected venv is not current after install')
    report['selected_venv'] = str(selected)
    report['packages_current'] = True
    check_fixtures(snapshots)
    generations = selected.parent.parent
    before = {path.name for path in generations.iterdir()}
    cfg_stat = (selected / 'pyvenv.cfg').stat()
    metadata_before = (HOME / 'plugins/.install-metadata.json').read_bytes()
    require(ensure_installed(entry) is False, 'Second check unexpectedly reinstalled provider')
    # Real PM no-op sync, without repair or any forced reinstall.
    sync_venv(explicit=True, project_root=SOURCE)
    require(venv_is_current(project_root=SOURCE) and selected_venv(SOURCE) == selected,
            'Idempotent PM sync changed the selected environment')
    require({path.name for path in generations.iterdir()} == before,
            'Idempotent PM sync created another generation')
    require((selected / 'pyvenv.cfg').stat().st_ino == cfg_stat.st_ino and
            (selected / 'pyvenv.cfg').stat().st_mtime_ns == cfg_stat.st_mtime_ns,
            'Idempotent PM sync rewrote the selected venv')
    require((HOME / 'plugins/.install-metadata.json').read_bytes() == metadata_before,
            'Idempotent check changed install metadata')
    report['idempotence'] = {'no_forced_reinstall': True, 'same_selected_generation': True,
                             'real_pm_noop_sync': True, 'packages_current': True}
    report['migration'] = run_worker('migrate', selected)
    report['independent_process_reopen'] = run_worker('read', selected)
    check_fixtures(snapshots)
    report['fixtures'] = {'default_config_unchanged': True, 'named_profile_config_unchanged': True,
                          'unrelated_plugin_metadata_preserved': True}
    report['selected_venv_allocation'] = allocated_tree(selected)
    report['pm_runtime_facts'] = str(runtime_facts_path(SOURCE))
    report['legacy_schema_sha256'] = hashlib.sha256(LEGACY_SCHEMA.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', choices=('migrate', 'read'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    started = time.monotonic()
    report = {'status': 'failed', 'probe': 'holographic-migration', 'synthetic_only': True}
    monitor = None
    try:
        safety_gate()
        report['safety_gate'] = 'passed'
        report['data_baseline'] = allocation()
        if not args.worker:
            monitor = PeakMonitor()
            monitor.thread.start()
        configure_environment()
        if args.worker:
            # No fixture creation or installation is reachable from this branch.
            print(json.dumps(worker(args.worker), sort_keys=True))
            return 0
        with contextlib.redirect_stdout(sys.stderr):
            integration(report)
        report['status'] = 'passed'
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        traceback.print_exc(file=sys.stderr)
    finally:
        if not args.worker:
            if monitor is not None:
                try:
                    monitor.finish()
                    report['data_final'] = allocation()
                    report['data_peak_used_bytes'] = monitor.peak
                    report['data_peak_increase_bytes'] = monitor.peak - report['data_baseline']['used_bytes']
                    report['data_final_increase_bytes'] = (report['data_final']['used_bytes'] -
                                                           report['data_baseline']['used_bytes'])
                    report['data_peak_sample_count'] = monitor.samples
                    report['data_peak_sample_interval_seconds'] = 0.2
                except BaseException as exc:
                    report['status'] = 'failed'
                    report['measurement_error'] = f'{type(exc).__name__}: {exc}'
            report['elapsed_seconds'] = round(time.monotonic() - started, 3)
            print('HOLOGRAPHIC_MIGRATION_REPORT=' + json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
