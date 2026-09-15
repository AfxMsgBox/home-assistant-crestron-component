"""Exercise deploy shell routing with fake GitHub/SSH/SCP/Node, never touching HA."""
import os
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DeployShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / 'commands.log'
        self.archive = self.directory / 'source.tar.gz'
        self.sha = 'a' * 40
        self.make_archive()
        self.env = dict(os.environ, PATH=f'{self.directory}:' + os.environ['PATH'],
                        DEPLOY_TEST_LOG=str(self.log), HA_SSH_PASSWORD='', HA_URL='',
                        DEPLOY_TEST_ARCHIVE=str(self.archive), DEPLOY_TEST_SHA=self.sha,
                        DEPLOY_TEST_CAPTURE=str(self.directory / 'capture'))
        curl = self.directory / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import json, os, shutil, sys
from urllib.parse import unquote
args = sys.argv[1:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as log:
    log.write('curl ' + ' '.join(args) + '\\n')
url = next(a for a in args if a.startswith(('https://', 'http://')))
if '/api/' in url:
    print(os.environ.get('PROBE_STATUS', '401'), end='')
    sys.exit(0)
if os.environ.get('FAIL_DOWNLOAD') == '1':
    sys.exit(22)
output = args[args.index('-o') + 1]
if '/git/ref/heads/' in url:
    branch = unquote(url.split('/git/ref/heads/', 1)[1])
    if os.environ.get('BAD_REF') == '1':
        branch = 'wrong'
    with open(output, 'w') as f:
        json.dump({'ref':'refs/heads/' + branch, 'object':{'sha':os.environ['DEPLOY_TEST_SHA']}}, f)
else:
    shutil.copyfile(os.environ['DEPLOY_TEST_ARCHIVE'], output)
''')
        curl.chmod(0o755)
        for name in ['ssh', 'scp', 'node']:
            stub = self.directory / name
            stub.write_text('''#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$DEPLOY_TEST_LOG"
case "$*" in
  *--check*) [ "${FAIL_VALIDATE:-}" != 1 ] || exit 1 ;;
  *--backup-dir*) [ "${FAIL_DASHBOARD:-}" != 1 ] || exit 1 ;;
  *"ha core check"*) [ "${FAIL_CORE_CHECK:-}" != 1 ] || exit 1 ;;
  *"ha core restart"*) [ "${FAIL_RESTART:-}" != 1 ] || exit 1 ;;
  *"ha core info --raw-json"*) printf '%s\\n' '{"data":{"port":8123,"ssl":false}}' ;;
  *"test -f"*manifest.json*) [ "${NOT_INSTALLED:-}" != 1 ] || exit 1 ;;
  *"backup_dir="*) [ "${FAIL_BACKUP:-}" != 1 ] || exit 1 ;;
esac
exit 0
''')
            stub.chmod(0o755)
        # Capture every uploaded tree before the deploy script cleans staging.
        scp = self.directory / 'scp'
        scp.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as log:
    log.write('scp ' + ' '.join(args) + '\\n')
source, destination = args[-2:]
capture = pathlib.Path(os.environ['DEPLOY_TEST_CAPTURE'])
name = 'source' if '/crestron-source/' in destination else ('component' if '-r' in args else 'yaml')
target = capture / name
if os.environ.get('FAIL_UPLOAD') == name:
    sys.exit(1)
capture.mkdir(exist_ok=True)
if pathlib.Path(source).is_dir():
    shutil.copytree(source, target, dirs_exist_ok=True)
else:
    shutil.copyfile(source, target)
''')
        scp.chmod(0o755)

    def make_archive(self, *, dashboards=True, extra=None, omit=None):
        files = {
            'custom_components/crestron/manifest.json': '{"domain":"crestron"}',
            'custom_components/crestron/climate.py': '# downloaded climate\n',
            'custom_components/crestron/crestron.yaml': 'do not deploy automatically',
            'custom_components/crestron/__pycache__/climate.pyc': 'cache',
            'tools/xlsx_to_yaml.py': 'from pathlib import Path\nimport sys\nPath(sys.argv[2]).write_text("downloaded converter output")\n',
            'README.md': 'downloaded documentation',
            'configuration.yaml': 'sample only',
            '.gitignore': '/local/',
        }
        if dashboards:
            files.update({'tools/deploy_dashboards.mjs': '// downloaded helper',
                          'dashboard/page-test.json': '{}'})
        files.update(extra or {})
        files.pop(omit, None)
        with tarfile.open(self.archive, 'w:gz') as archive:
            for path, content in files.items():
                data = content.encode()
                info = tarfile.TarInfo('repo-' + self.sha + '/' + path)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def run_script(self, *args, input_text='', script=None):
        result = subprocess.run(
            ['bash', str(script or ROOT / 'tools/deploy_to_ha.sh'), '--host', 'ha.example',
             '--user', 'root', '--key', *args], env=self.env,
            input=input_text, text=True, capture_output=True, timeout=15)
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

    def test_default_install_downloads_master_and_uploads_complete_source(self):
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/git/ref/heads/master' in line for line in log))
        self.assertTrue(any('/tar.gz/' + self.sha in line for line in log))
        capture = self.directory / 'capture'
        self.assertEqual((capture / 'component/climate.py').read_text(), '# downloaded climate\n')
        self.assertFalse((capture / 'component/crestron.yaml').exists())
        self.assertFalse((capture / 'component/__pycache__').exists())
        for name in ['tools/xlsx_to_yaml.py', 'dashboard/page-test.json', '.gitignore', 'README.md', 'configuration.yaml']:
            self.assertTrue((capture / 'source' / name).is_file(), name)
        self.assertTrue(any('--check' in line and '/source/repo-' in line for line in log))
        self.assertTrue(any('--backup-dir' in line and '/source/repo-' in line for line in log))
        self.assertTrue(any('http://ha.example:8123/api/' in line for line in log))
        self.assertFalse(any('ha host reboot' in line for line in log))

    def test_update_defaults_preserve_yaml_and_requires_existing_component(self):
        result, log = self.run_script('--update', '--branch', 'dev')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/git/ref/heads/dev' in line for line in log))
        self.assertTrue(any('test -f' in line and 'manifest.json' in line for line in log))
        self.assertFalse((self.directory / 'capture/yaml').exists())
        self.assertNotIn('是否重新', result.stdout)

    def test_missing_installation_does_not_upload_or_restart(self):
        self.env['NOT_INSTALLED'] = '1'
        result, log = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--install', result.stderr)
        self.assertFalse(any(line.startswith('scp ') or 'ha core restart' in line for line in log))

    def test_slash_and_unicode_in_branch_are_url_encoded(self):
        result, log = self.run_script('--update', '--branch', 'feature/温控')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/heads/feature%2F%E6%B8%A9%E6%8E%A7' in line for line in log))

    def test_invalid_branch_is_rejected_before_network_access(self):
        for branch in ['../dev', '-dev', 'a b', 'dev?x=y', 'a//b', 'a.lock', '@', 'a@{b']:
            result, log = self.run_script('--update', '--branch', branch)
            self.assertNotEqual(result.returncode, 0, branch)
            self.assertEqual(log, [])

    def test_mutually_exclusive_install_update(self):
        result, log = self.run_script('--install', '--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log, [])

    def test_download_or_ref_failure_does_not_touch_server(self):
        for failure in ['FAIL_DOWNLOAD', 'BAD_REF']:
            self.env[failure] = '1'
            result, log = self.run_script('--update')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))
            self.env.pop(failure)

    def test_invalid_archive_does_not_touch_server(self):
        for archive_bytes in [b'not gzip', b'']:
            self.archive.write_bytes(archive_bytes)
            result, log = self.run_script('--update')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))

    def test_traversal_link_or_multiple_roots_are_rejected(self):
        for kind in ['traversal', 'symlink', 'roots']:
            with tarfile.open(self.archive, 'w:gz') as archive:
                info = tarfile.TarInfo('root/../escape' if kind == 'traversal' else 'root/link')
                if kind == 'symlink':
                    info.type = tarfile.SYMTYPE
                    info.linkname = '/tmp/escape'
                archive.addfile(info, io.BytesIO())
                if kind == 'roots':
                    archive.addfile(tarfile.TarInfo('second/file'), io.BytesIO())
            result, log = self.run_script('--update')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))

    def test_missing_required_archive_file_does_not_touch_server(self):
        self.make_archive(omit='custom_components/crestron/manifest.json')
        result, log = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))

    def test_older_branch_without_dashboard_is_supported(self):
        self.make_archive(dashboards=False)
        result, log = self.run_script('--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('不包含 dashboard', result.stdout)
        self.assertFalse(any(line.startswith('node ') for line in log))

    def test_dashboard_only_requires_dashboard_in_selected_branch(self):
        self.make_archive(dashboards=False)
        result, log = self.run_script('--dashboard-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))

    def test_generate_uses_downloaded_converter_and_explicit_local_inputs(self):
        workbook = self.directory / 'device table.xlsx'
        workbook.write_bytes(b'fixture')
        yaml = self.directory / 'device config.yaml'
        result, log = self.run_script('--update', '--xlsx', str(workbook), '--yaml', str(yaml), '--upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(yaml.read_text(), 'downloaded converter output')
        self.assertEqual((self.directory / 'capture/yaml').read_text(), yaml.read_text())

    def test_backup_failure_prevents_component_overwrite(self):
        self.env['FAIL_BACKUP'] = '1'
        result, log = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / 'capture/component').exists())
        self.assertFalse(any('ha core restart' in line for line in log))

    def test_source_upload_failure_prevents_component_overwrite(self):
        self.env['FAIL_UPLOAD'] = 'source'
        result, log = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / 'capture/component').exists())
        self.assertFalse(any('ha core restart' in line for line in log))

    def test_restart_failure_does_not_report_success(self):
        self.env['FAIL_RESTART'] = '1'
        result, _ = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('部署完成：', result.stdout)

    def test_http_probe_timeout_does_not_report_success(self):
        self.env['PROBE_STATUS'] = '503'
        result, _ = self.run_script('--update', '--restart-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未在 1 秒内恢复', result.stderr)
        self.assertNotIn('部署完成：', result.stdout)

    def test_explicit_ha_url_is_used_for_recovery_check(self):
        result, log = self.run_script('--update', '--ha-url', 'https://ha.example/proxy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('https://ha.example/proxy/api/' in line for line in log))

    def test_invalid_ha_url_stops_before_upload_or_restart(self):
        result, log = self.run_script('--update', '--skip-dashboard', '--ha-url', 'ftp://ha.example')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(line.startswith('scp ') or 'ha core restart' in line for line in log))
        self.assertFalse(any('mkdir -p' in line or 'backup_dir=' in line for line in log))

    def test_script_can_run_outside_a_checkout(self):
        standalone = self.directory / 'standalone/deploy_to_ha.sh'
        standalone.parent.mkdir()
        standalone.write_text((ROOT / 'tools/deploy_to_ha.sh').read_text())
        result, _ = self.run_script('--update', script=standalone)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
