"""Exercise deploy shell routing with fake SSH/SCP/Node, never touching HA."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeployShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / 'commands.log'
        self.env = dict(os.environ, PATH=f'{self.directory}:' + os.environ['PATH'],
                        DEPLOY_TEST_LOG=str(self.log), HA_SSH_PASSWORD='')
        for name in ['ssh', 'scp', 'node']:
            stub = self.directory / name
            stub.write_text('''#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$DEPLOY_TEST_LOG"
case "$*" in
  *--check*) [ "${FAIL_VALIDATE:-}" != 1 ] || exit 1 ;;
  *--backup-dir*) [ "${FAIL_DASHBOARD:-}" != 1 ] || exit 1 ;;
  *"ha core check"*) [ "${FAIL_CORE_CHECK:-}" != 1 ] || exit 1 ;;
esac
exit 0
''')
            stub.chmod(0o755)

    def run_script(self, *args):
        result = subprocess.run(
            ['bash', str(ROOT / 'tools/deploy_to_ha.sh'), '--host', 'ha.example',
             '--user', 'root', '--key', *args], env=self.env,
            input='', text=True, capture_output=True, timeout=10)
        log = self.log.read_text().splitlines() if self.log.exists() else []
        return result, log

    def test_dashboard_only_skips_component_yaml_check_and_restart(self):
        result, log = self.run_script('--dashboard-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(line.startswith('scp ') for line in log))
        self.assertFalse(any('ha core check' in line or 'ha core restart' in line for line in log))
        self.assertTrue(any('--backup-dir' in line for line in log))
        self.assertNotIn('是否重新', result.stdout)

    def test_regular_flow_checks_then_deploys_dashboard_then_restarts(self):
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        check = next(i for i, line in enumerate(log) if 'ha core check' in line)
        dashboard = next(i for i, line in enumerate(log) if '--backup-dir' in line)
        restart = next(i for i, line in enumerate(log) if 'ha core restart' in line)
        self.assertLess(check, dashboard)
        self.assertLess(dashboard, restart)

    def test_skip_dashboard_does_not_require_node(self):
        result, log = self.run_script('--skip-dashboard', '--skip-generate-yaml', '--skip-upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(line.startswith('node ') for line in log))
        self.assertTrue(any('ha core restart' in line for line in log))

    def test_invalid_dashboard_stops_before_any_ssh_or_upload(self):
        self.env['FAIL_VALIDATE'] = '1'
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))

    def test_failed_core_check_stops_dashboard_and_restart(self):
        self.env['FAIL_CORE_CHECK'] = '1'
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('--backup-dir' in line or 'ha core restart' in line for line in log))

    def test_failed_dashboard_stops_restart(self):
        self.env['FAIL_DASHBOARD'] = '1'
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('ha core restart' in line for line in log))

    def test_conflicting_flags_fail_before_remote_access(self):
        for flag in ['--skip-dashboard', '--generate-yaml', '--upload-yaml']:
            result, log = self.run_script('--dashboard-only', flag)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log, [])


if __name__ == '__main__':
    unittest.main()
