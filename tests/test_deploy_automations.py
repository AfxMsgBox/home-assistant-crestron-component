"""第二阶段部署测试：真实备份/替换 shell 在本机临时目录执行，绝不连接 HA。"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import deploy_automations as d
from test_xlsx_to_automations import workbook, registry


class AutomationDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ha = self.root / 'ha'
        self.ha.mkdir()
        self.backup = '/homeassistant/crestron-backups/2026-10-11/120000-test'
        (self.ha / self.backup.removeprefix('/homeassistant/')).mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        cmd = self.bin / 'ha'
        cmd.write_text('#!/bin/sh\n[ "${FAIL_CHECK:-}" != 1 ]\n')
        cmd.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'])
        self.config = 'automation: !include automations.yaml\n# 保留手工注释\n'.encode()

    def run_upload(self, old_manual=b'old manual', *, fail=False, concurrent=False):
        # 将工具构造的 SSH 脚本直接在本机执行，保留标准输入压缩包及真实 tar/mv/cp。
        (self.ha / 'configuration.yaml').write_bytes(self.config)
        if old_manual is not None:
            (self.ha / 'automations_manual.yaml').write_bytes(old_manual)
        if concurrent:
            (self.ha / 'configuration.yaml').write_text('concurrent user edit')
        new = d.include_config(self.config)
        def execute(command, script, content=None, timeout=45):
            result = subprocess.run(['sh', '-c', script.replace('/homeassistant', str(self.ha))],
                                    input=content, capture_output=True,
                                    env=dict(self.env, FAIL_CHECK='1' if fail else '0'), timeout=5)
            if result.returncode:
                raise d.generator.AssociationError('模拟远端失败')
            return result.stdout
        with patch.object(d, 'remote', side_effect=execute):
            d.upload(['ssh'], self.backup, 'new manual\n', self.config, old_manual, new)
        return new

    def test_backup_install_and_cleanup(self):
        new = self.run_upload()
        self.assertEqual((self.ha / 'configuration.yaml').read_bytes(), new)
        self.assertEqual((self.ha / 'automations_manual.yaml').read_text(), 'new manual\n')
        backup = self.ha / self.backup.removeprefix('/homeassistant/') / 'automation'
        self.assertEqual((backup / 'configuration.yaml').read_bytes(), self.config)
        self.assertEqual((backup / 'automations_manual.yaml').read_bytes(), b'old manual')
        self.assertFalse(list(self.ha.glob('.crestron-automation.*')))
        self.assertEqual((self.ha / 'automations_manual.yaml').stat().st_mode & 0o777, 0o600)

    def test_first_install_records_absence(self):
        self.run_upload(None)
        backup = self.ha / self.backup.removeprefix('/homeassistant/') / 'automation'
        self.assertTrue((backup / 'manual-was-absent').is_file())

    def test_failed_check_restores_both_files(self):
        with self.assertRaises(d.generator.AssociationError):
            self.run_upload(fail=True)
        self.assertEqual((self.ha / 'configuration.yaml').read_bytes(), self.config)
        self.assertEqual((self.ha / 'automations_manual.yaml').read_bytes(), b'old manual')
        self.assertFalse(list(self.ha.glob('.crestron-automation.*')))

    def test_failed_first_install_removes_new_file(self):
        with self.assertRaises(d.generator.AssociationError):
            self.run_upload(None, fail=True)
        self.assertFalse((self.ha / 'automations_manual.yaml').exists())
        self.assertEqual((self.ha / 'configuration.yaml').read_bytes(), self.config)

    def test_concurrent_edit_blocks_overwrite(self):
        with self.assertRaises(d.generator.AssociationError):
            self.run_upload(concurrent=True)
        self.assertEqual((self.ha / 'configuration.yaml').read_text(), 'concurrent user edit')
        self.assertEqual((self.ha / 'automations_manual.yaml').read_bytes(), b'old manual')

    def test_include_is_idempotent_and_conflicts_rejected(self):
        new = d.include_config(self.config)
        self.assertEqual(d.include_config(new), new)
        self.assertTrue(new.startswith(self.config))
        for content in [b'automation manual: !include other.yaml\n',
                        b'automation other: !include automations_manual.yaml\n',
                        new + new]:
            with self.assertRaises(d.generator.AssociationError):
                d.include_config(content)

    def test_backup_path_injection_rejected(self):
        with self.assertRaises(d.generator.AssociationError):
            d.install_script('/tmp/anything', self.config, None, True)

    def test_skip_and_eof_upload(self):
        self.assertFalse(d.ask_upload(True, False, read=lambda _: 'n'))
        def eof(_):
            raise EOFError
        self.assertFalse(d.ask_upload(False, True, read=eof))
        self.assertTrue(d.ask_upload(True, True, read=lambda _: 'y'))

    def test_api_credentials_only_in_stdin_and_reload_endpoint(self):
        seen = []
        def remote(command, script, content=None, timeout=45):
            seen.append((script, content))
            return b'[]'
        with patch.dict(os.environ, HA_TOKEN='test-secret'), patch.object(d, 'remote', side_effect=remote):
            api = d.Api(['ssh', 'server'], 'http://ha:80')
            api.request('api/services/automation/reload', post=True)
        self.assertNotIn('test-secret', seen[0][0])
        self.assertIn(b'test-secret', seen[0][1])
        self.assertIn(b'http://ha:80/api/services/automation/reload', seen[0][1])
        self.assertIn(b'request = "POST"', seen[0][1])

    def test_missing_registry_no_upload_or_output_on_timeout(self):
        xlsx = self.root / 'test.xlsx'
        workbook(xlsx)
        output = self.root / 'automations_manual.yaml'
        output.write_text('keep me')
        with patch.object(d, 'Api'), patch.object(d, 'read_registry', return_value={'entities': []}), \
             patch.object(d, 'upload') as upload, patch('sys.stdout', new=io.StringIO()):
            with self.assertRaisesRegex(d.generator.AssociationError, '等待实体超时'):
                d.main(['--xlsx', str(xlsx), '--output', str(output), '--ha-url', 'http://ha',
                        '--wait-seconds', '0', '--upload', 'yes', '--', 'ssh', 'server'])
        upload.assert_not_called()
        self.assertEqual(output.read_text(), 'keep me')

    def test_wait_then_generate_only(self):
        xlsx = self.root / 'test.xlsx'
        workbook(xlsx)
        output = self.root / 'automations_manual.yaml'
        with patch.object(d, 'Api'), patch.object(d, 'read_registry', side_effect=[{'entities': []}, registry()]), \
             patch.object(d.time, 'sleep'), patch.object(d, 'upload') as upload, patch('sys.stdout', new=io.StringIO()):
            result = d.main(['--xlsx', str(xlsx), '--output', str(output), '--ha-url', 'http://ha',
                             '--upload', 'no', '--', 'ssh', 'server'])
        self.assertEqual(result, 0)
        self.assertIn('light.renamed', output.read_text())
        upload.assert_not_called()

    def test_independent_stage_creates_backup_only_after_upload_confirmation(self):
        xlsx = self.root / 'test.xlsx'
        workbook(xlsx)
        output = self.root / 'automations_manual.yaml'
        for accepted in (False, True):
            with self.subTest(accepted=accepted), \
                 patch.object(d, 'Api') as api, \
                 patch.object(d, 'read_registry', return_value=registry()), \
                 patch.object(d, 'read_optional', side_effect=[self.config, b'old automation']), \
                 patch.object(d, 'ask_upload', return_value=accepted), \
                 patch.object(d.common, 'create_backup', return_value=self.backup) as backup, \
                 patch.object(d, 'upload') as upload, patch('sys.stdout', new=io.StringIO()):
                api.return_value.request.return_value = [dict(entity_id='automation.link', state='on',
                                                              attributes={'id': 'crestron_manual_associations'})]
                result = d.main(['--xlsx', str(xlsx), '--output', str(output), '--ha-url', 'http://ha',
                                 '--', 'ssh', 'server'])
                self.assertEqual(result, 0)
                if accepted:
                    backup.assert_called_once_with(['ssh', 'server'])
                    self.assertEqual(upload.call_args.args[1], self.backup)
                else:
                    backup.assert_not_called()
                    upload.assert_not_called()

    def test_prepared_second_step_reuses_first_step_backup_and_url(self):
        context = self.root / 'device-result.json'
        context.write_text(json.dumps({'backup_dir': self.backup, 'ha_url': 'http://ha:80'}))
        with patch.object(d.common, 'Connection') as connection, \
             patch.object(d.common, 'core_url', return_value='http://ha:80') as url, \
             patch.object(d, 'main', return_value=0) as main:
            connection.return_value.ssh = ['ssh', 'server']
            connection.return_value.env = {}
            result = d.entry(['--prepared', '--host', 'ha', '--xlsx', 'test.xlsx',
                              '--output', 'auto.yaml', '--context', str(context)])
        self.assertEqual(result, 0)
        url.assert_called_once_with(connection.return_value, 'http://ha:80')
        argv = main.call_args.args[0]
        self.assertEqual(argv[argv.index('--backup-dir') + 1], self.backup)
        self.assertEqual(argv[-3:], ['--', 'ssh', 'server'])


if __name__ == '__main__':
    unittest.main()
