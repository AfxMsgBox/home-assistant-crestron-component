"""在临时目录运行真实清理 shell；HA 命令由替身接管，不访问服务器。"""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import clear_deleted_devices as d


@unittest.skipUnless(shutil.which('jq'), '需要 jq 执行远端脚本测试')
class CleanupShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ha = self.root / 'homeassistant'
        (self.ha / '.storage').mkdir(parents=True)
        self.registry = self.ha / '.storage/core.device_registry'
        self.original = {'key': 'core.device_registry', 'version': 3, 'minor_version': 4,
                         'data': {'devices': [{'id': 'active', 'name': '正在使用的设备'}],
                                  'child_devices': {'keep': ['child']},
                                  'deleted_devices': [{'id': 'old1'}, {'id': 'old2'}]},
                         'unknown': {'future': True}}
        self.registry.write_text(json.dumps(self.original, ensure_ascii=False) + '\n')
        self.registry.chmod(0o640)
        self.before = self.registry.read_bytes()
        self.entity_registry = self.ha / '.storage/core.entity_registry'
        self.entity_original = {'key': 'core.entity_registry', 'version': 1,
                                'data': {'entities': [{'entity_id': 'climate.example_active', 'unique_id': 'active'}],
                                         'deleted_entities': [{'entity_id': 'climate.example_old', 'unique_id': 'old'}],
                                         'settings': {'keep': True}}, 'future': 123}
        self.entity_registry.write_text(json.dumps(self.entity_original) + '\n')
        self.entity_registry.chmod(0o600)
        self.entity_before = self.entity_registry.read_bytes()
        self.state = self.root / 'state'
        self.state.write_text('running')
        self.log = self.root / 'log'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        TEST_ROOT=str(self.root), TEST_MODE='')
        self.program('ha', '''import json, os, sys
from pathlib import Path
root = Path(os.environ['TEST_ROOT'])
state = root / 'state'
command = sys.argv[2]
mode = os.environ['TEST_MODE']
with (root / 'log').open('a') as log:
    log.write(command + '\\n')
if command == 'stats':
    if mode == 'stats_unknown':
        print(json.dumps({'result': 'error', 'message': 'Timeout getting Home Assistant stats'}))
        sys.exit(1)
    if state.read_text() == 'stopped':
        print(json.dumps({'result': 'error', 'message': 'Home Assistant is not running'}))
        sys.exit(1)
    print(json.dumps({'result': 'ok', 'data': {'cpu_percent': 1}}))
elif command == 'stop':
    if mode != 'stop_lies':
        state.write_text('stopped')
    if mode == 'stop_fails':
        print(json.dumps({'result': 'error'}))
        sys.exit(1)
    if mode == 'stop_changes':
        registry = root / 'homeassistant/.storage/core.device_registry'
        content = json.loads(registry.read_text())
        content['data']['deleted_devices'].append({'id': 'unconfirmed'})
        registry.write_text(json.dumps(content))
    print(json.dumps({'result': 'ok'}))
elif command == 'start':
    if mode == 'start_fails':
        print(json.dumps({'result': 'error'}))
        sys.exit(1)
    state.write_text('running')
    print(json.dumps({'result': 'ok'}))
else:
    sys.exit(2)
''')

    def program(self, name, body):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)

    def shell(self, script):
        return subprocess.run(['sh', '-c', script.replace('/homeassistant', str(self.ha))],
                              env=self.env, text=True, capture_output=True, timeout=15)

    def preview(self, target='devices'):
        result = self.shell(d.preview_script(target))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def cleanup(self, mode='', preview=None, target='devices'):
        preview = preview or self.preview(target)
        self.env['TEST_MODE'] = mode
        return self.shell(d.cleanup_script(preview))

    def backups(self):
        return list(self.ha.glob('crestron-backups/*/*/device-registry/core.device_registry'))

    def assert_clean_temporary(self):
        self.assertFalse((self.ha / '.crestron-device-cleanup.lock').exists())
        self.assertFalse(list((self.ha / '.storage').glob('.device-cleanup.*')))

    def test_success_preserves_everything_else_and_full_backup(self):
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = copy.deepcopy(self.original)
        expected['data']['deleted_devices'] = []
        self.assertEqual(json.loads(self.registry.read_text()), expected)
        self.assertEqual(len(self.backups()), 1)
        backup = self.backups()[0]
        self.assertEqual(backup.read_bytes(), self.before)
        self.assertRegex(str(backup.relative_to(self.ha)), r'^crestron-backups/\d{4}-\d{2}-\d{2}/\d{6}-[^/]+/device-registry/core.device_registry$')
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.state.read_text(), 'running')
        self.assertEqual([x for x in self.log.read_text().splitlines() if x != 'stats'], ['stop', 'start'])
        self.assert_clean_temporary()

    def test_already_stopped_stays_stopped(self):
        self.state.write_text('stopped')
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), 'stopped')
        self.assertTrue(all(x == 'stats' for x in self.log.read_text().splitlines()))

    def test_empty_or_missing_list_needs_no_core_query(self):
        for missing in (False, True):
            content = copy.deepcopy(self.original)
            content['data']['deleted_devices'] = []
            if missing:
                del content['data']['deleted_devices']
            self.registry.write_text(json.dumps(content))
            self.assertEqual(self.preview()['count'], 0)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.backups())

    def test_unknown_stats_is_not_stopped(self):
        self.env['TEST_MODE'] = 'stats_unknown'
        result = self.shell(d.preview_script())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)

    def test_timeout_with_partial_error_output_is_not_stopped(self):
        self.program('timeout', '''import sys
print('{"result":"error","message":"Home Assistant is not running"}')
sys.exit(124)
''')
        result = self.shell(d.preview_script())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)

    def test_stop_failure_restores_running_without_modification(self):
        result = self.cleanup('stop_fails')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertEqual(self.state.read_text(), 'running')
        self.assertFalse(self.backups())
        self.assert_clean_temporary()

    def test_stop_success_response_but_still_running_is_rejected(self):
        result = self.cleanup('stop_lies')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertFalse(self.backups())
        self.assertNotIn('start', self.log.read_text())

    def test_deleted_records_changed_during_stop_requires_new_confirmation(self):
        result = self.cleanup('stop_changes')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(json.loads(self.registry.read_text())['data']['deleted_devices']), 3)
        self.assertEqual(self.state.read_text(), 'running')
        self.assertFalse(self.backups())

    def test_backup_failure_does_not_change_registry(self):
        cp = shutil.which('cp')
        self.program('cp', f'''import os, sys
if '/device-registry/' in sys.argv[-1]:
    sys.exit(1)
os.execv({cp!r}, [{cp!r}, *sys.argv[1:]])
''')
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertEqual(self.state.read_text(), 'running')
        self.assert_clean_temporary()

    def test_post_write_validation_failure_rolls_back_exact_bytes(self):
        jq = shutil.which('jq')
        self.program('jq', f'''import os, sys
if '-s' in sys.argv and sys.argv[-1].endswith('/.storage/core.device_registry'):
    sys.exit(1)
os.execv({jq!r}, [{jq!r}, *sys.argv[1:]])
''')
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertEqual(self.backups()[0].read_bytes(), self.before)
        self.assertEqual(self.state.read_text(), 'running')
        self.assertIn('已恢复原设备注册表', result.stderr)
        self.assert_clean_temporary()

    def test_external_edit_after_replace_is_not_overwritten_by_rollback(self):
        mv = shutil.which('mv')
        self.program('mv', f'''import subprocess, sys
from pathlib import Path
result = subprocess.run([{mv!r}, *sys.argv[1:]])
if result.returncode == 0 and sys.argv[-2].endswith('/new'):
    Path(sys.argv[-1]).write_text('{{"external": true}}')
sys.exit(result.returncode)
''')
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.registry.read_text()), {'external': True})
        self.assertEqual(self.backups()[0].read_bytes(), self.before)
        self.assertEqual(self.state.read_text(), 'stopped')
        self.assertIn('无法安全自动恢复', result.stderr)

    def test_start_failure_keeps_cleaned_file_and_backup(self):
        result = self.cleanup('start_fails')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.registry.read_text())['data']['deleted_devices'], [])
        self.assertEqual(self.backups()[0].read_bytes(), self.before)
        self.assertEqual(self.log.read_text().splitlines().count('start'), 1)
        self.assert_clean_temporary()

    def test_confirmation_stale_and_existing_lock_block_changes(self):
        preview = self.preview()
        content = copy.deepcopy(self.original)
        content['data']['deleted_devices'].append({'id': 'new'})
        self.registry.write_text(json.dumps(content))
        result = self.cleanup(preview=preview)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('stop', self.log.read_text())
        lock = self.ha / '.crestron-device-cleanup.lock'
        lock.mkdir()
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.is_dir())
        self.assertNotIn('stop', self.log.read_text())

    def test_symlink_and_invalid_list_are_rejected(self):
        content = copy.deepcopy(self.original)
        content['data']['deleted_devices'] = None
        self.registry.write_text(json.dumps(content))
        self.assertNotEqual(self.shell(d.preview_script()).returncode, 0)
        self.registry.unlink()
        target = self.root / 'other'
        target.write_bytes(self.before)
        self.registry.symlink_to(target)
        self.assertNotEqual(self.shell(d.preview_script()).returncode, 0)
        self.assertEqual(target.read_bytes(), self.before)

    def test_entities_only_preserves_device_registry(self):
        preview = self.preview('entities')
        self.assertEqual(preview['count'], 1)
        result = self.cleanup(preview=preview)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.registry.read_bytes(), self.before)
        expected = copy.deepcopy(self.entity_original)
        expected['data']['deleted_entities'] = []
        self.assertEqual(json.loads(self.entity_registry.read_text()), expected)
        backups = list(self.ha.glob('crestron-backups/*/*/entity-registry/core.entity_registry'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.entity_before)
        self.assertEqual(self.entity_registry.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.backups())

    def test_all_preserves_active_records_and_backs_up_both_before_restarting_once(self):
        preview = self.preview('all')
        self.assertEqual(preview['count'], 3)
        self.assertEqual([row['count'] for row in preview['registries']], [2, 1])
        result = self.cleanup(preview=preview)
        self.assertEqual(result.returncode, 0, result.stderr)
        for file, original, field in [(self.registry, self.original, 'deleted_devices'),
                                      (self.entity_registry, self.entity_original, 'deleted_entities')]:
            expected = copy.deepcopy(original)
            expected['data'][field] = []
            self.assertEqual(json.loads(file.read_text()), expected)
        entity_backup = next(self.ha.glob('crestron-backups/*/*/entity-registry/core.entity_registry'))
        self.assertEqual(entity_backup.parent.parent, self.backups()[0].parent.parent)
        self.assertEqual(entity_backup.read_bytes(), self.entity_before)
        self.assertEqual([x for x in self.log.read_text().splitlines() if x != 'stats'], ['stop', 'start'])
        self.assert_clean_temporary()

    def test_all_with_empty_device_list_keeps_its_exact_bytes(self):
        original = copy.deepcopy(self.original)
        del original['data']['deleted_devices']
        before = json.dumps(original).encode()
        self.registry.write_bytes(before)
        result = self.cleanup(target='all')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.backups()[0].read_bytes(), before)
        self.assertEqual(json.loads(self.entity_registry.read_text())['data']['deleted_entities'], [])

    def test_all_empty_skips_core_query(self):
        for file, field in [(self.registry, 'deleted_devices'), (self.entity_registry, 'deleted_entities')]:
            content = json.loads(file.read_text())
            content['data'][field] = []
            file.write_text(json.dumps(content))
        preview = self.preview('all')
        self.assertEqual((preview['count'], preview['state']), (0, 'unchecked'))
        self.assertFalse(self.log.exists())
        self.assertFalse(self.backups())

    def test_second_backup_failure_changes_neither_file(self):
        cp = shutil.which('cp')
        self.program('cp', f'''import os, sys
if '/entity-registry/' in sys.argv[-1]: sys.exit(1)
os.execv({cp!r}, [{cp!r}, *sys.argv[1:]])
''')
        result = self.cleanup(target='all')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertEqual(self.entity_registry.read_bytes(), self.entity_before)
        self.assertEqual(self.state.read_text(), 'running')
        self.assert_clean_temporary()

    def test_second_write_failure_restores_first_file(self):
        mv = shutil.which('mv')
        self.program('mv', f'''import os, sys
if sys.argv[-2].endswith('/entities/new'): sys.exit(1)
os.execv({mv!r}, [{mv!r}, *sys.argv[1:]])
''')
        result = self.cleanup(target='all')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertEqual(self.entity_registry.read_bytes(), self.entity_before)
        self.assertEqual(self.state.read_text(), 'running')
        self.assert_clean_temporary()

    def test_second_validation_failure_restores_both_exact_files(self):
        jq = shutil.which('jq')
        self.program('jq', f'''import os, sys
if '-s' in sys.argv and sys.argv[-1].endswith('/.storage/core.entity_registry'): sys.exit(1)
os.execv({jq!r}, [{jq!r}, *sys.argv[1:]])
''')
        result = self.cleanup(target='all')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertEqual(self.entity_registry.read_bytes(), self.entity_before)
        self.assertEqual(self.state.read_text(), 'running')
        self.assert_clean_temporary()

    def test_external_change_to_second_file_stops_rollback_and_restart(self):
        mv = shutil.which('mv')
        self.program('mv', f'''import subprocess, sys
from pathlib import Path
result = subprocess.run([{mv!r}, *sys.argv[1:]])
if result.returncode == 0 and sys.argv[-2].endswith('/entities/new'):
    Path(sys.argv[-1]).write_text('{{"external": true}}')
sys.exit(result.returncode)
''')
        result = self.cleanup(target='all')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.entity_registry.read_text()), {'external': True})
        self.assertEqual(self.state.read_text(), 'stopped')
        self.assertIn('无法安全自动恢复', result.stderr)
        self.assertNotIn('start', self.log.read_text().splitlines())

    def test_entity_collection_changed_after_confirmation_prevents_stop(self):
        preview = self.preview('all')
        content = copy.deepcopy(self.entity_original)
        content['data']['deleted_entities'].append({'entity_id': 'climate.example_added'})
        self.entity_registry.write_text(json.dumps(content))
        result = self.cleanup(preview=preview)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('stop', self.log.read_text().splitlines())
        self.assertEqual(self.registry.read_bytes(), self.before)

    def test_invalid_unselected_registry_is_ignored_but_selected_registry_blocks_all(self):
        self.entity_registry.write_text('{}')
        self.assertEqual(self.preview()['count'], 2)
        result = self.shell(d.preview_script('all'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.registry.read_bytes(), self.before)
        self.assertFalse(self.backups())

    def test_entity_symlink_is_rejected(self):
        target = self.root / 'entity-copy'
        target.write_bytes(self.entity_before)
        self.entity_registry.unlink()
        self.entity_registry.symlink_to(target)
        result = self.shell(d.preview_script('all'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), self.entity_before)



class CleanupCliTests(unittest.TestCase):
    def invoke(self, flags, state='running', count=2, accepted=True):
        target = flags[flags.index('--target') + 1] if '--target' in flags else 'devices'
        kinds = d.targets(target)
        preview = dict(target=target, count=count * len(kinds), state=state if count else 'unchecked',
                       registries=[dict(target=kind, count=count, digest='a' * 64) for kind in kinds])
        with patch.object(d.common, 'Connection') as connection, \
             patch.object(d.common, 'core_url', return_value='http://ha:80') as url, \
             patch.object(d.common, 'wait_for_http') as wait, \
             patch.object(d.common, 'ask_yes_no', return_value=accepted) as ask, \
             patch('sys.stdout', new=io.StringIO()):
            connection.return_value.remote.return_value = json.dumps(preview)
            result = d.main(['--host', 'ha', *flags])
        return result, connection.return_value.remote, url, wait, ask

    def test_dry_run_cancel_and_empty_do_not_write(self):
        for flags, count, accepted in [(['--dry-run'], 2, True), ([], 2, False), ([], 0, True)]:
            result, remote, url, wait, ask = self.invoke(flags, count=count, accepted=accepted)
            self.assertEqual(result, 0)
            self.assertEqual(remote.call_count, 1)
            wait.assert_not_called()

    def test_wait_only_when_previously_running(self):
        for state in ('running', 'stopped'):
            result, remote, url, wait, ask = self.invoke(['--yes'], state=state)
            self.assertEqual(result, 0)
            self.assertEqual(remote.call_count, 2)
            ask.assert_not_called()
            self.assertEqual(wait.call_count, int(state == 'running'))
            self.assertEqual(url.call_count, int(state == 'running'))

    def test_interactive_password_and_default_key(self):
        for password in ('secret', ''):
            replies = ['ha', '', '', *([] if password else ['']), '']
            with patch('builtins.input', side_effect=replies), patch.object(d.getpass, 'getpass', return_value=password):
                args = d.parse_args([])
            self.assertEqual((args.host, args.user, args.port), ('ha', 'root', 22))
            self.assertEqual(args.password, password)
            self.assertIsNone(args.identity)
            self.assertEqual(args.target, 'devices')

    def test_cli_scope_selects_only_requested_registry_and_all_is_explicit(self):
        for target in ('devices', 'entities', 'all'):
            result, remote, _, _, _ = self.invoke(['--yes', '--target', target])
            self.assertEqual(result, 0)
            script = remote.call_args_list[0].args[0]
            self.assertIn("target=" + target, script)
        self.assertEqual(d.parse_args(['--host', 'ha']).target, 'devices')

    def test_interactive_selects_all_and_retries_invalid_input(self):
        with patch('builtins.input', side_effect=['ha', '', '', 'bad', '3']), \
             patch.object(d.getpass, 'getpass', return_value='password'), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(d.parse_args([]).target, 'all')

    def test_malformed_or_incomplete_preview_is_rejected(self):
        valid = dict(target='all', count=2, state='running', registries=[
            dict(target=kind, count=1, digest='a' * 64) for kind in ('devices', 'entities')])
        d.validate_preview(valid, 'all')
        for mutate in [lambda p: p.update(count=True), lambda p: p.update(count=1),
                       lambda p: p['registries'].pop(), lambda p: p['registries'][1].update(digest='bad'),
                       lambda p: p['registries'][1].update(target='devices'), lambda p: p.update(state='unchecked')]:
            value = copy.deepcopy(valid)
            mutate(value)
            with self.assertRaises(d.common.DeployError):
                d.cleanup_script(value)
        with self.assertRaises(d.common.DeployError):
            d.validate_preview(valid, 'devices')


if __name__ == '__main__':
    unittest.main()
