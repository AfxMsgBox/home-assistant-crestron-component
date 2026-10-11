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

    def preview(self):
        result = self.shell(d.preview_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def cleanup(self, mode='', preview=None):
        preview = preview or self.preview()
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


class CleanupCliTests(unittest.TestCase):
    def invoke(self, flags, state='running', count=2, accepted=True):
        preview = dict(count=count, digest='a' * 64, state=state)
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
            replies = ['ha', '', '', *([] if password else [''])]
            with patch('builtins.input', side_effect=replies), patch.object(d.getpass, 'getpass', return_value=password):
                args = d.parse_args([])
            self.assertEqual((args.host, args.user, args.port), ('ha', 'root', 22))
            self.assertEqual(args.password, password)
            self.assertIsNone(args.identity)


if __name__ == '__main__':
    unittest.main()
