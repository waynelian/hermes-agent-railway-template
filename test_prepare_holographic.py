"""Offline fixtures only. Run with the image Python: -B -m unittest -v test_prepare_holographic."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import yaml

import prepare_holographic as boot


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / '.hermes'
        self.source = Path(self.temp.name) / 'source'
        self.home.mkdir()
        self.source.mkdir()
        (self.home / 'plugins').mkdir()
        (self.source / 'install-stamp.json').write_text(json.dumps({
            'displayVersion': '0.22.5', 'commit': boot.SOURCE_COMMIT}))
        self.config = {'memory': {'provider': 'holographic', 'privacy': {'retain': True}},
                       'plugins': {'enabled': ['disk-cleanup', 'openai-codex', 'url-reputation'],
                                   'disabled': [], 'hermes-memory-store': {
                                       'db_path': '$HERMES_HOME/memory_store.db', 'auto_extract': False},
                                   'sentinel': {'values': [1, False, 'unrelated']}},
                       'model': {'api_key': '${DO_NOT_EXPAND_ME}'}, 'unrelated': {'nested': [1, 3]}}
        self.write_config(self.config)
        self.db = self.home / 'memory_store.db'
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE facts (fact_id INTEGER PRIMARY KEY, content TEXT, tags TEXT, '
                       'trust_score REAL, retrieval_count INTEGER, hrr_vector BLOB)')
            db.execute('INSERT INTO facts VALUES (13, ?, ?, 0.7, 4, ?)',
                       ('fixture-content', 'fixture-tags', b'old-vector'))
        self.meta_path = self.home / 'plugins/.install-metadata.json'
        self.meta_path.write_text(json.dumps({'sentinel': {'keep': True}}))
        self.runtime = Mock(spec=boot.Runtime)
        self.runtime.current.return_value = True
        self.runtime.extras.return_value = set(boot.REQUIRED_EXTRAS)
        self.runtime.entry.return_value = types.SimpleNamespace(sha=boot.PLUGIN_COMMIT)
        self.runtime.write_config.side_effect = self.write_config
        self.runtime.install.side_effect = self.install
        self.space = patch.object(boot.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=10**12))
        self.space.start()
        self.addCleanup(self.space.stop)

    def write_config(self, config):
        (self.home / 'config.yaml').write_text(yaml.safe_dump(config, sort_keys=False))

    def install(self, entry):
        (self.home / 'plugins/holographic').mkdir()
        data = json.loads(self.meta_path.read_text())
        data['holographic'] = {'revision': boot.PLUGIN_COMMIT}
        self.meta_path.write_text(json.dumps(data))

    def prepare(self):
        return boot.prepare(self.home, self.source, self.runtime)

    def installed(self):
        (self.home / 'plugins/holographic').mkdir()

    def test_no_config_no_runtime_or_checkpoint(self):
        # Move rather than delete, to make the no-config state explicit.
        (self.home / 'config.yaml').rename(self.home / 'fixture-config.saved')
        self.assertEqual(self.prepare(), 'no-config')
        self.assertEqual(self.runtime.mock_calls, [])
        self.assertFalse((self.home / boot.CHECKPOINT_NAME).exists())

    def test_other_provider_no_runtime(self):
        self.config['memory']['provider'] = 'none'
        self.write_config(self.config)
        self.assertEqual(self.prepare(), 'other-provider')
        self.assertEqual(self.runtime.mock_calls, [])

    def test_wrong_version_fails_before_checkpoint_or_install(self):
        (self.source / 'install-stamp.json').write_text('{}')
        with self.assertRaisesRegex(boot.PreparationError, 'reviewed-source'):
            self.prepare()
        self.runtime.install.assert_not_called()
        self.assertFalse((self.home / boot.CHECKPOINT_NAME).exists())

    def test_checkpoint_private_idempotent_small_and_sqlite_backup(self):
        # A session-state sentinel must not be read/copied.
        (self.home / 'state.db').write_bytes(b'not-a-memory-db')
        before = boot.fact_snapshot(self.db)
        target = boot.checkpoint(self.home, self.db)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertNotIn('state.db', {p.name for p in target.iterdir()})
        files = {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in target.iterdir()}
        for path in target.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(boot.fact_snapshot(target / 'memory_store.db'), before)
        self.config['runtime-added'] = True
        self.write_config(self.config)
        self.assertEqual(boot.checkpoint(self.home, self.db), target)
        self.assertEqual(files, {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                                 for p in target.iterdir()})

    def test_incomplete_or_tampered_checkpoint_fails_closed(self):
        target = self.home / boot.CHECKPOINT_NAME
        target.mkdir(mode=0o700)
        with self.assertRaisesRegex(boot.PreparationError, 'incomplete-checkpoint'):
            self.prepare()
        self.runtime.install.assert_not_called()

    def test_checkpoint_tamper(self):
        target = boot.checkpoint(self.home, self.db)
        (target / 'config.yaml').write_text('changed')
        with self.assertRaisesRegex(boot.PreparationError, 'checkpoint-content'):
            self.prepare()
        self.runtime.install.assert_not_called()

    def test_install_and_config_preserve_all_unrelated_values(self):
        before = copy.deepcopy(self.config)
        facts = boot.fact_snapshot(self.db)
        self.assertEqual(self.prepare(), 'installed-and-verified')
        self.assertEqual(boot.read_config(self.home / 'config.yaml'), boot.desired_config(before))
        settings = boot.read_config(self.home / 'config.yaml')['plugins']['hermes-memory-store']
        self.assertTrue(settings['mirror_memory_writes'])
        self.assertFalse(settings['recall_indicator'])
        self.assertNotIn('embeddings', settings)
        self.assertNotIn('scope', settings)
        self.assertEqual(boot.fact_snapshot(self.db), facts)
        self.assertTrue(json.loads(self.meta_path.read_text())['sentinel']['keep'])
        self.assertEqual(self.prepare(), 'verified-existing')
        self.assertEqual(self.runtime.install.call_count, 1)
        self.assertEqual(self.runtime.write_config.call_count, 1)

    def test_explicit_privacy_settings_are_not_overridden(self):
        settings = self.config['plugins']['hermes-memory-store']
        settings.update(mirror_memory_writes=False, recall_indicator=True, embeddings='off',
                        scope='profile', cron_writes=False, hrr_dim=512)
        self.write_config(self.config)
        self.prepare()
        self.assertEqual(boot.read_config(self.home / 'config.yaml'), self.config)
        self.runtime.write_config.assert_not_called()

    def test_pinned_existing_skips_install_even_with_low_space(self):
        self.installed()
        boot.checkpoint(self.home, self.db)
        with patch.object(boot.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=1)):
            self.assertEqual(self.prepare(), 'verified-existing')
        self.runtime.install.assert_not_called()

    def test_stale_existing_never_rebuilds(self):
        self.installed()
        self.runtime.current.return_value = False
        with self.assertRaisesRegex(boot.PreparationError, 'stale-manual-review'):
            self.prepare()
        self.runtime.install.assert_not_called()
        self.runtime.write_config.assert_not_called()

    def test_mismatched_existing_never_reinstalls(self):
        self.installed()
        self.runtime.verify_install.side_effect = boot.PreparationError('installed-plugin-provenance-mismatch')
        with self.assertRaisesRegex(boot.PreparationError, 'provenance-mismatch'):
            self.prepare()
        self.runtime.install.assert_not_called()

    def test_orphaned_metadata_never_reinstalls(self):
        self.meta_path.write_text(json.dumps({'holographic': {'revision': 'wrong'}}))
        with self.assertRaisesRegex(boot.PreparationError, 'orphaned-plugin-record'):
            self.prepare()
        self.runtime.install.assert_not_called()

    def test_low_headroom_checked_after_checkpoint_before_install(self):
        with patch.object(boot.shutil, 'disk_usage', return_value=types.SimpleNamespace(
                free=boot.ENVIRONMENT_BYTES + boot.DEFAULT_RESERVE_BYTES - 1)):
            with self.assertRaisesRegex(boot.PreparationError, 'insufficient-environment-headroom'):
                self.prepare()
        self.assertTrue((self.home / boot.CHECKPOINT_NAME / 'manifest.json').is_file())
        self.runtime.install.assert_not_called()
        self.runtime.write_config.assert_not_called()

    def test_reserve_cannot_be_lowered(self):
        with self.assertRaisesRegex(boot.PreparationError, 'reserve-below'):
            boot.prepare(self.home, self.source, self.runtime, reserve_bytes=0)

    def test_lost_extras_stop_before_migration(self):
        self.runtime.extras.side_effect = [set(boot.REQUIRED_EXTRAS), {'all'}]
        with self.assertRaisesRegex(boot.PreparationError, 'existing-extras-lost'):
            self.prepare()
        self.runtime.migrate.assert_not_called()

    def test_migration_fact_metadata_changes_block_startup(self):
        def mutate():
            with contextlib.closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE facts SET retrieval_count=99')
        self.runtime.migrate.side_effect = mutate
        with self.assertRaisesRegex(boot.PreparationError, 'original-facts-or-metadata-changed'):
            self.prepare()
        self.assertTrue((self.home / boot.CHECKPOINT_NAME / 'memory_store.db').exists())

    def test_unrelated_plugin_metadata_changes_block(self):
        def mutate(entry):
            self.install(entry)
            self.meta_path.write_text(json.dumps({'sentinel': {'keep': False}}))
        self.runtime.install.side_effect = mutate
        with self.assertRaisesRegex(boot.PreparationError, 'unrelated-plugin-metadata-changed'):
            self.prepare()
        self.runtime.migrate.assert_not_called()

    def test_config_installer_changes_block_before_write(self):
        def mutate(entry):
            self.install(entry)
            self.write_config({'memory': {'provider': 'none'}})
        self.runtime.install.side_effect = mutate
        with self.assertRaisesRegex(boot.PreparationError, 'installer-changed-config'):
            self.prepare()
        self.runtime.write_config.assert_not_called()

    def test_default_home_and_selection_safety(self):
        self.assertEqual(boot.validate_home(self.home, self.home, self.home, {}), self.home)
        for env in ({'HERMES_PROFILE': 'other'}, {'HERMES_CONFIG': '/other'},
                    {'HERMES_ENABLE_PROJECT_PLUGINS': '1'}):
            with self.assertRaises(boot.PreparationError):
                boot.validate_home(self.home, self.home, self.home, env)
        named = self.home / 'profiles/named'
        with self.assertRaises(boot.PreparationError):
            boot.validate_home(named, self.home, named, {})
        link = self.home.parent / 'redirect'
        link.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(boot.PreparationError):
            boot.validate_home(link, self.home, self.home, {})

    def test_database_path_safety_and_expansion(self):
        for raw in ('${HERMES_HOME}/memory_store.db', str(self.db)):
            cfg = copy.deepcopy(self.config)
            cfg['plugins']['hermes-memory-store']['db_path'] = raw
            self.assertEqual(boot.database_path(self.home, cfg), self.db)
        for raw in ('state.db', '$HERMES_HOME/state.db', '$HERMES_HOME/profiles/other/facts.db',
                    '/outside/facts.db', '$UNKNOWN/facts.db', ':memory:'):
            cfg = copy.deepcopy(self.config)
            cfg['plugins']['hermes-memory-store']['db_path'] = raw
            with self.assertRaises(boot.PreparationError):
                boot.database_path(self.home, cfg)

    def test_atomic_writer_and_normal_catalog_api_contract(self):
        writer, installer = Mock(), Mock(return_value=(self.home / 'plugins/holographic',
                                                       {'name': 'holographic'}, 'holographic'))
        modules = {'hermes_cli.config': types.SimpleNamespace(atomic_config_write=writer),
                   'hermes_cli.plugins_cmd_catalog': types.SimpleNamespace(install_catalog_entry=installer)}
        runtime = boot.Runtime(self.home, self.source)
        entry = types.SimpleNamespace(sha=boot.PLUGIN_COMMIT)
        with patch.dict('sys.modules', modules):
            runtime.write_config(self.config)
            runtime.install(entry)
        writer.assert_called_once_with(self.home / 'config.yaml', self.config)
        installer.assert_called_once_with(entry, force=False, ref=boot.PLUGIN_COMMIT,
                                           assume_deps_consent=True, python_deps=True)

    def test_actual_admission_readback_contract(self):
        self.installed()
        entry = types.SimpleNamespace(sha=boot.PLUGIN_COMMIT, name='holographic',
                                      repo=boot.PLUGIN_REPO, tier='community')
        catalog = {'name': entry.name, 'repo': entry.repo, 'tier': entry.tier,
                   'sha': entry.sha, 'pin': entry.sha}
        record = {'revision': entry.sha, 'source': entry.repo, 'pinned': True, 'catalog': catalog}
        self.meta_path.write_text(json.dumps({'holographic': record}))
        sidecar = {('catalog_name' if k == 'name' else k): v for k, v in catalog.items()}
        modules = {'hermes_cli.plugins_cmd_catalog': types.SimpleNamespace(
                       catalog_install_record=Mock(return_value=sidecar)),
                   'plugins.memory': types.SimpleNamespace(find_provider_dir=Mock(
                       return_value=self.home / 'plugins/holographic'))}
        responses = [types.SimpleNamespace(returncode=0, stdout=entry.sha + '\n'),
                     types.SimpleNamespace(returncode=0, stdout='')]
        runtime = boot.Runtime(self.home, self.source)
        with patch.dict('sys.modules', modules), patch.object(boot.subprocess, 'run', side_effect=responses):
            runtime.verify_install(entry)
        record['revision'] = 'wrong'
        self.meta_path.write_text(json.dumps({'holographic': record}))
        with patch.dict('sys.modules', modules), patch.object(boot.subprocess, 'run') as run:
            with self.assertRaisesRegex(boot.PreparationError, 'provenance-mismatch'):
                runtime.verify_install(entry)
            run.assert_not_called()

    def test_catalog_uses_canonical_repository_url(self):
        directory = self.source / 'plugin-catalog'
        entry = types.SimpleNamespace(sha=boot.PLUGIN_COMMIT, name='holographic',
            repo='https://github.com/itpartypattaya/hermes-plugin-holographic')
        modules = {'hermes_cli.plugin_catalog': types.SimpleNamespace(
            get_catalog_dir=lambda: directory, get_catalog_entry=Mock(return_value=entry))}
        with patch.dict('sys.modules', modules):
            self.assertIs(boot.Runtime(self.home, self.source).entry(), entry)

    def test_extras_fallback_to_baked_selection_and_runtime_precedence(self):
        runtime_path = self.home / 'runtime-facts.json'
        baked_path = self.source / 'baked-facts.json'
        records = {runtime_path: None, baked_path: {'extras': sorted(boot.REQUIRED_EXTRAS)}}
        facts = Mock(side_effect=lambda path, strict: types.SimpleNamespace(get=lambda name: records[path]))
        features = Mock(side_effect=AssertionError('Must not replace recorded extras with anchor inventory'))
        modules = {'pm.environments': types.SimpleNamespace(runtime_facts_path=lambda source: runtime_path),
                   'pm.paths': types.SimpleNamespace(facts_path=lambda: baked_path),
                   'pm.lock': types.SimpleNamespace(Facts=facts),
                   'pm.features': types.SimpleNamespace(read_features=features)}
        with patch.dict('sys.modules', modules):
            runtime = boot.Runtime(self.home, self.source)
            self.assertEqual(runtime.extras(), set(boot.REQUIRED_EXTRAS))
            records[runtime_path] = {'extras': sorted(boot.REQUIRED_EXTRAS | {'extra-fixture'})}
            self.assertIn('extra-fixture', runtime.extras())
        features.assert_not_called()

    def test_selected_interpreter_command_contract(self):
        selected = self.home / 'fake-generation/venv'
        prefix = ['sealed-python', '-B', 'sealed-entry', 'site-packages']
        command = Mock(return_value=prefix)
        modules = {'pm.environments': types.SimpleNamespace(
            selected_venv=Mock(return_value=selected), venv_command=command)}
        with patch.dict('sys.modules', modules), patch.object(boot.subprocess, 'run',
                return_value=types.SimpleNamespace(returncode=0)) as run:
            boot.Runtime(self.home, self.source).migrate()
        command.assert_called_once_with(self.source, selected, options=['-B'])
        self.assertEqual(run.call_args.args[0], [*prefix, str(Path(boot.__file__).resolve()), '--worker'])
        self.assertEqual(run.call_args.kwargs['stdout'], boot.subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['stderr'], boot.subprocess.DEVNULL)

    def test_worker_opens_store_without_provider_lifecycle_or_embedding(self):
        store = Mock()
        store.read.return_value = [('fixture-version',)]
        factory = Mock(return_value=store)
        module = types.SimpleNamespace(__file__=str(self.home / 'plugins/holographic/store.py'),
            sqlite3=sqlite3, MemoryStore=factory, SCHEMA_VERSION='fixture-version')
        importer = Mock(return_value=module)
        modules = {'pm.environments': types.SimpleNamespace(running_from_selected_environment=lambda source: True),
                   'pm.extras': types.SimpleNamespace(available=lambda extra: True),
                   'plugins.memory': types.SimpleNamespace(import_provider_module=importer)}
        with patch.dict('sys.modules', modules), patch.object(boot.Runtime, 'current', return_value=True), \
                patch.object(boot.Runtime, 'extras', return_value=set(boot.REQUIRED_EXTRAS)), \
                patch.object(boot.sys, 'addaudithook') as audit:
            boot.migration_worker(self.home, self.source)
        audit.assert_called_once_with(boot.offline_guard)
        importer.assert_called_once_with('holographic', 'store')
        factory.assert_called_once_with(db_path=self.db, default_trust=0.5, hrr_dim=1024)
        store.close.assert_called_once_with()
        store.initialize.assert_not_called()
        store.handle_tool_call.assert_not_called()
        self.assertEqual(len(store.mock_calls), 2)  # read schema version, then close

    def test_wal_checkpoint_includes_committed_uncheckpointed_rows(self):
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('INSERT INTO facts VALUES (14, ?, ?, 0.9, 1, NULL)', ('wal-fixture', 'wal'))
            db.commit()
            self.assertTrue(Path(str(self.db) + '-wal').exists())
            target = boot.checkpoint(self.home, self.db)
            self.assertEqual(boot.fact_snapshot(target / 'memory_store.db'), boot.fact_snapshot(self.db))

    def test_cli_failures_never_print_private_exception_values(self):
        constants = types.SimpleNamespace(get_default_hermes_root=lambda: self.home,
                                         get_hermes_home=lambda: self.home)
        output = io.StringIO()
        with patch.dict('sys.modules', {'hermes_constants': constants}), \
                patch.dict(os.environ, {'HERMES_HOME': str(self.home), 'HERMES_PROFILE': 'default'}, clear=True), \
                patch.object(boot.sys, 'argv', ['prepare_holographic.py']), \
                patch.object(boot, 'quiet_output', contextlib.nullcontext), \
                patch.object(boot, 'prepare', side_effect=ValueError('private-fixture-value')), \
                contextlib.redirect_stderr(output):
            self.assertEqual(boot.main(), 1)
        self.assertNotIn('private-fixture-value', output.getvalue())
        self.assertIn('unexpected-error-manual-review-required', output.getvalue())

    def test_offline_audit_guard(self):
        for event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'):
            with self.assertRaises(boot.PreparationError):
                boot.offline_guard(event, ())
        boot.offline_guard('sqlite3.connect', ())


if __name__ == '__main__':
    unittest.main()
