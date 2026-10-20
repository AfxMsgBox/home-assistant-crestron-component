"""页面独立工具：首次资源初始化、共用备份、只读预检；不连接现场。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import deploy_dashboards as d


class DashboardEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'tools').mkdir()
        (self.root / 'tools/deploy_dashboards.mjs').write_text('// fixture')
        (self.root / 'dashboard').mkdir()
        (self.root / 'dashboard/page.json').write_text(json.dumps({'resources': ['resources/card.js']}))
        self.args = ['--prepared', '--source-root', str(self.root), '--host', 'ha.example']
        self.connection = MagicMock()
        self.connection.ssh = ['ssh', 'root@ha.example']
        self.connection.env = {}
        self.connection.remote.return_value = 'existing'
        self.calls = []
        self.backup = '/homeassistant/crestron-backups/2026-10-20/120000-Test12'
        for name, replacement in [
            ('require', MagicMock()), ('run', MagicMock(side_effect=lambda *a, **k: self.calls.append(a[0]))),
            ('Connection', MagicMock(return_value=self.connection)),
            ('create_backup', MagicMock(return_value=self.backup)),
            ('core_url', MagicMock(return_value='http://ha.example:8123')),
            ('wait_for_http', MagicMock()), ('message', MagicMock()),
        ]:
            started = patch.object(d.common, name, replacement)
            started.start()
            self.addCleanup(started.stop)

    def test_check_does_not_connect_or_create_backup(self):
        self.assertEqual(d.main([*self.args, '--check']), 0)
        d.common.Connection.assert_not_called()
        d.common.create_backup.assert_not_called()
        self.assertTrue(any('--check' in command for command in self.calls))

    def test_existing_www_does_not_restart_and_reuses_context(self):
        context = self.root / 'context.json'
        context.write_text(json.dumps({'backup_dir': self.backup, 'ha_url': 'http://ha.example:8123'}))
        self.assertEqual(d.main([*self.args, '--context', str(context)]), 0)
        d.common.create_backup.assert_not_called()
        d.common.wait_for_http.assert_not_called()
        self.assertIn(self.backup + '/dashboard', self.calls[-1])
        self.assertIn('http://ha.example:8123', self.calls[-1])
        self.assertFalse(any('restart' in str(call) for call in self.connection.remote.call_args_list))

    def test_initial_www_requires_confirmation_before_mutation(self):
        self.connection.remote.return_value = 'missing'
        with patch.object(d.common, 'ask_yes_no', return_value=False) as ask:
            with self.assertRaisesRegex(d.common.DeployError, '取消初始化'):
                d.main(self.args)
        ask.assert_called_once()
        self.assertEqual(ask.call_args.kwargs['default'], True)
        self.assertEqual(self.connection.remote.call_count, 1)
        d.common.create_backup.assert_not_called()

    def test_confirmed_initial_www_checks_then_creates_restarts_and_waits(self):
        self.connection.remote.return_value = 'missing'
        with patch.object(d.common, 'ask_yes_no', return_value=True):
            self.assertEqual(d.main(self.args), 0)
        commands = [call.args[0] for call in self.connection.remote.call_args_list]
        self.assertEqual(commands[1:], ['ha core check', 'mkdir -p /homeassistant/www', 'ha core restart'])
        d.common.wait_for_http.assert_called_once()
        self.assertIn('--remote-backup-dir', self.calls[-1])

    def test_unknown_directory_probe_stops_without_restart_or_upload(self):
        self.connection.remote.return_value = 'unexpected'
        with self.assertRaisesRegex(d.common.DeployError, '无法确认'):
            d.main(self.args)
        self.assertEqual(self.connection.remote.call_count, 1)
        self.assertEqual(len(self.calls), 2)
        d.common.create_backup.assert_not_called()

    def test_missing_node_or_assets_stop_before_connection(self):
        (self.root / 'tools/deploy_dashboards.mjs').unlink()
        with self.assertRaises(d.common.DeployError):
            d.main(self.args)
        d.common.Connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
