"""Exercise deploy shell routing with fake GitHub/SSH/SCP/Node, never touching HA."""
import os
import io
import json
from pathlib import Path
import subprocess
import sys
import shutil
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REAL_SCP = shutil.which("scp")


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
                        DEPLOY_TEST_CAPTURE=str(self.directory / 'capture'),
                        DEPLOY_TEST_REMOTE=str(self.directory / 'remote'), TMPDIR=str(self.directory))
        curl = self.directory / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import json, os, shutil, sys
from urllib.parse import unquote, urlsplit, parse_qs
args = sys.argv[1:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as log:
    log.write(('remote-curl ' if os.environ.get('REMOTE_PROBE') else 'curl ') + ' '.join(args) + '\\n')
url = next(a for a in args if a.startswith(('https://', 'http://')))
if '/api/' in url:
    if not os.environ.get('REMOTE_PROBE'):
        sys.exit('本机代理无法访问 HA')
    if os.environ.get('EXPECTED_PROBE_URL'):
        assert url == os.environ['EXPECTED_PROBE_URL'], repr(url)
    statuses = os.environ.get('PROBE_STATUSES')
    if statuses:
        from pathlib import Path
        counter = Path(os.environ['DEPLOY_TEST_LOG'] + '.probes')
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        values = statuses.split(',')
        status = values[min(count, len(values) - 1)]
    else:
        status = os.environ.get('PROBE_STATUS', '401')
    print(status, end='')
    if os.environ.get('PROBE_ERROR'):
        print(os.environ['PROBE_ERROR'], file=sys.stderr)
    sys.exit(int(os.environ.get('MOCK_PROBE_EXIT', '0')))
if os.environ.get('FAIL_DOWNLOAD') == '1':
    sys.exit(22)
output = args[args.index('-o') + 1]
if '/branches?' in url:
    if os.environ.get('FAIL_BRANCHES') == '1':
        sys.exit(22)
    names = json.loads(os.environ.get('TEST_BRANCHES', '["dev", "master"]'))
    page = int(parse_qs(urlsplit(url).query)['page'][0])
    response = [{'name': name} for name in names[(page-1)*100:page*100]]
    if os.environ.get('BAD_BRANCHES') == '1':
        response = {'message': 'bad response'}
    with open(output, 'w') as f:
        json.dump(response, f)
elif '/git/ref/heads/' in url:
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
  *"command -v curl"*) [ "${MISSING_REMOTE_CURL:-}" != 1 ] || exit 127 ;;
  *"curl -sS"*)
    if [ "${FAIL_PROBE_SSH:-}" = 1 ]; then echo 'SSH connection lost' >&2; exit 255; fi
    for arg do remote_command=$arg; done
    REMOTE_PROBE=1 sh -c "$remote_command"
    exit $? ;;
  *--check*)
    [ "${FAIL_VALIDATE:-}" != 1 ] || exit 1
    if [ -n "${MUTATE_LOCAL_FILE:-}" ]; then printf 'changed after staging' > "$MUTATE_LOCAL_FILE"; fi ;;

  *--remote-backup-dir*) [ "${FAIL_DASHBOARD:-}" != 1 ] || exit 1 ;;
  *"ha core check"*) [ "${FAIL_CORE_CHECK:-}" != 1 ] || exit 1 ;;
  *"ha core restart"*) [ "${FAIL_RESTART:-}" != 1 ] || exit 1 ;;
  *"ha core info --raw-json"*) printf '%s\\n' '{"data":{"port":8123,"ssl":false}}' ;;
  *"test -f"*manifest.json*) [ "${NOT_INSTALLED:-}" != 1 ] || exit 1 ;;
  *"backup_dir="*) [ "${FAIL_BACKUP:-}" != 1 ] || exit 1 ;;
esac
exit 0
''')
            stub.chmod(0o755)
        remote_component = self.directory / 'remote/custom_components/crestron'
        remote_component.mkdir(parents=True)
        (remote_component / 'manifest.json').write_text('{"version":"old"}')
        (remote_component / 'crestron.yaml').write_text('original site configuration')
        (remote_component / '.hidden').write_text('preserve hidden files')
        ssh = self.directory / 'ssh'
        ssh.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
command = args[-1]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as log:
    log.write('ssh ' + ' '.join(args).replace('\\n', ' ') + '\\n')
if 'curl -sS' in command:
    if os.environ.get('FAIL_PROBE_SSH') == '1':
        sys.exit('SSH connection lost')
    result = subprocess.run(['sh', '-c', command], env=dict(os.environ, REMOTE_PROBE='1'))
    sys.exit(result.returncode)
for fragment, flag in [('command -v curl', 'MISSING_REMOTE_CURL'), ('ha core check', 'FAIL_CORE_CHECK'),
                       ('ha core restart', 'FAIL_RESTART'), ('test -f', 'NOT_INSTALLED'),
                       ('backup_dir=', 'FAIL_BACKUP'), ('stamp=$(date', 'FAIL_BACKUP_DIR')]:
    if fragment in command and os.environ.get(flag) == '1':
        sys.exit(1)
if 'ha core info --raw-json' in command:
    print('{"data":{"port":8123,"ssl":false}}')
elif 'stamp=$(date' in command or 'backup_dir=' in command:
    # Execute the actual remote backup shell locally, remapping only its root.
    root = os.environ['DEPLOY_TEST_REMOTE']
    result = subprocess.run(['sh', '-c', command.replace('/homeassistant', root)],
                            text=True, capture_output=True)
    sys.stdout.write(result.stdout.replace(root, '/homeassistant'))
    sys.stderr.write(result.stderr)
    sys.exit(result.returncode)
''')
        ssh.chmod(0o755)
        tar = self.directory / 'tar'
        tar.write_text('#!/bin/sh\n'
                       'if [ "${FAIL_TAR:-}" = 1 ] && [ "$1" = -czf ]; then printf partial > "$2"; exit 1; fi\n'
                       'if [ "${FAIL_TAR_VERIFY:-}" = 1 ] && [ "$1" = -tzf ]; then exit 1; fi\n'
                       'exec ' + shutil.which('tar') + ' "$@"\n')
        tar.chmod(0o755)
        # Capture every uploaded tree before the deploy script cleans staging.
        scp = self.directory / 'scp'
        scp.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, subprocess, sys
args = sys.argv[1:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as log:
    log.write('scp ' + ' '.join(args) + '\\n')
# Parse flags instead of assuming one source path: archive uploads include
# every top-level file/directory, including hidden entries.
position = 0
while position < len(args) and args[position].startswith('-'):
    position += 2 if args[position] in ('-P', '-o', '-i') else 1
sources, destination = args[position:-1], args[-1]
capture = pathlib.Path(os.environ['DEPLOY_TEST_CAPTURE'])
name = 'source' if '/crestron-source/' in destination else ('component' if '-r' in args else 'yaml')
target = capture / name
if os.environ.get('FAIL_UPLOAD') == name:
    sys.exit(1)
capture.mkdir(exist_ok=True)
if os.environ.get('REAL_SCP') and name == 'component':
    # Exercise the real legacy SCP sender/receiver locally, using pipes instead
    # of SSH/network. The deployment's actual source arguments are preserved.
    target = capture
    ack_read, ack_write = os.pipe()
    data_read, data_write = os.pipe()
    children = []
    try:
        receiver = subprocess.Popen(
            [os.environ['REAL_SCP'], '-O', '-r', '-p', '-t', str(target)],
            stdin=data_read, stdout=ack_write, stderr=subprocess.PIPE)
        children.append(receiver)
        sender = subprocess.Popen(
            [os.environ['REAL_SCP'], '-O', '-r', '-p', '-f', *sources],
            stdin=ack_read, stdout=data_write, stderr=subprocess.PIPE)
        children.append(sender)
    finally:
        for fd in (ack_read, ack_write, data_read, data_write):
            os.close(fd)
    try:
        for child in children:
            _, error = child.communicate(timeout=5)
            sys.stderr.buffer.write(error)
        failed = any(child.returncode != 0 for child in children)
        if not failed:
            (capture / 'crestron').rename(capture / 'component')
        sys.exit(int(failed))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
for source in sources:
    if source.rstrip('/').rsplit('/', 1)[-1] == '.':
        sys.exit('scp: error: unexpected filename: .')
    source_path = pathlib.Path(source)
    destination_path = target / source_path.name if name == 'source' else target
    if name == 'source':
        target.mkdir(exist_ok=True)
    if source_path.is_dir():
        shutil.copytree(source, destination_path, dirs_exist_ok=True)
    else:
        shutil.copyfile(source, destination_path)
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
            'test/old_test.py': '# development only',
            'tests/test_climate.py': '# development only',
        }
        for name in ('deploy_common.py', 'deploy_devices.py', 'deploy_dashboards.py'):
            files['tools/' + name] = (ROOT / 'tools' / name).read_text()
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

    def run_script(self, *args, input_text='', script=None, interactive=False):
        connection = [] if interactive else ['--host', 'ha.example', '--user', 'root', '--key']
        if not interactive and '--source' not in args and script is None:
            connection += ['--source', 'github']
        result = subprocess.run(
            ['bash', str(script or ROOT / 'tools/deploy_to_ha.sh'), *connection, *args],
            env=self.env, cwd=self.directory,
            input=input_text, text=True, capture_output=True, timeout=15)
        log = self.log.read_text().splitlines() if self.log.exists() else []
        return result, log

    def make_local_source(self):
        root = self.directory / '本地 checkout'
        root.mkdir()
        with tarfile.open(self.archive) as archive:
            for member in archive.getmembers():
                relative = Path(*Path(member.name).parts[1:])
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        script = root / 'tools/deploy_to_ha.sh'
        shutil.copy2(ROOT / 'tools/deploy_to_ha.sh', script)
        (root / 'custom_components/crestron/climate.py').write_text('# local unsaved changes')
        (root / 'custom_components/crestron/new_module.py').write_text('# untracked file')
        (root / 'tools/xlsx_to_yaml.py').write_text(
            'from pathlib import Path\nimport sys\nPath(sys.argv[2]).write_text("local converter output")\n')
        for relative in ('.git/config', 'local/site.xlsx', '.venv/bin/python',
                         'node_modules/package/index.js', '.env', '.env.secret',
                         '.mypy_cache/cache', '.pytest_cache/cache'):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('not source')
        return root, script

    def automation_sources(self):
        self.make_archive(extra={
            'automations_manual_template.yaml': 'template from selected source',
            'tools/xlsx_to_automations.py': '''import os
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as f: f.write('automation-preflight\\n')
''',
            'tools/deploy_automations.py': '''import os, sys
from pathlib import Path
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as f: f.write('automation-stage2 ' + ' '.join(sys.argv[1:]) + '\\n')
Path(sys.argv[sys.argv.index('--output') + 1]).write_text('generated associations')
if os.environ.get('FAIL_AUTOMATIONS'): sys.exit(1)
''',
        })
        xlsx = self.directory / 'associations.xlsx'
        xlsx.write_text('fixture input')
        return xlsx

    def test_second_stage_follows_device_restart_and_http_recovery(self):
        xlsx = self.automation_sources()
        output = self.directory / 'automatic.yaml'
        result, log = self.run_script('--xlsx', str(xlsx), '--yaml', str(self.directory / 'devices.yaml'),
                                      '--skip-upload-yaml', '--generate-automations',
                                      '--automation-yaml', str(output), '--skip-upload-automations')
        self.assertEqual(result.returncode, 0, result.stderr)
        stage = next(i for i, line in enumerate(log) if line.startswith('automation-stage2'))
        probe = next(i for i, line in enumerate(log) if line.startswith('remote-curl'))
        restart = next(i for i, line in enumerate(log) if 'ha core restart' in line)
        self.assertLess(restart, probe)
        self.assertLess(probe, stage)
        self.assertIn('--upload no', log[stage])
        self.assertIn('/source/repo-' + self.sha, log[stage])
        self.assertEqual(output.read_text(), 'generated associations')

    def test_automation_only_skips_component_scp_and_restart(self):
        xlsx = self.automation_sources()
        result, log = self.run_script('--automations-only', '--xlsx', str(xlsx),
                                      '--automation-yaml', str(self.directory / 'auto.yaml'),
                                      '--skip-upload-automations')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(line.startswith('automation-stage2') for line in log))
        self.assertFalse(any(line.startswith('scp ') or 'ha core restart' in line for line in log))
        self.assertFalse(any('deploy_dashboards.mjs' in line for line in log))

    def test_second_stage_failure_is_not_reported_as_success(self):
        xlsx = self.automation_sources()
        self.env['FAIL_AUTOMATIONS'] = '1'
        result, _ = self.run_script('--automations-only', '--xlsx', str(xlsx),
                                    '--automation-yaml', str(self.directory / 'auto.yaml'),
                                    '--skip-upload-automations')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('部署完成：', result.stdout)

    def test_old_source_missing_automation_tool_stops_before_upload(self):
        xlsx = self.directory / 'source.xlsx'
        xlsx.write_text('fixture')
        result, log = self.run_script('--generate-automations', '--xlsx', str(xlsx))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('缺少自动化工具/模板', result.stderr)
        self.assertFalse(any(line.startswith('scp ') for line in log))

    def test_automation_skipped_when_user_declines(self):
        result, log = self.run_script('--update', input_text='n\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('已跳过自动化关联', result.stdout)
        self.assertFalse(any(line.startswith('automation-stage2') for line in log))

    def test_local_snapshot_includes_worktree_and_excludes_runtime_files(self):
        root, script = self.make_local_source()
        result, log = self.run_script('--source', 'local', '--update', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('github.com' in line for line in log))
        self.assertFalse(any('crestron-source' in line for line in log))
        capture = self.directory / 'capture'
        for tree in ('component',):
            self.assertEqual((capture / tree / 'climate.py').read_text(), '# local unsaved changes')
            self.assertTrue((capture / tree / 'new_module.py').is_file())
            self.assertFalse((capture / tree / 'crestron.yaml').exists())
            self.assertFalse((capture / tree / '__pycache__').exists())
        for name in ('.git', 'local', '.venv', 'node_modules', '.env', '.env.secret',
                     '.mypy_cache', '.pytest_cache', 'test', 'tests'):
            self.assertFalse((capture / 'source' / name).exists(), name)
        self.assertTrue((root / 'tests/test_climate.py').is_file())
        self.assertTrue((root / 'local/site.xlsx').exists())
        self.assertTrue((root / 'custom_components/crestron/crestron.yaml').exists())
        self.assertTrue(any('--check' in line and '/source/local/' in line for line in log))
        self.assertTrue(any('ha core restart' in line for line in log))
        self.assertIn(str(root), result.stdout)

    def test_local_generate_and_upload_use_local_converter(self):
        root, script = self.make_local_source()
        workbook = root / 'local/site.xlsx'
        output = root / 'local/output.yaml'
        result, _ = self.run_script('--source', 'local', '--update', '--xlsx', str(workbook),
                                   '--yaml', str(output), '--upload-yaml', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'capture/yaml').read_text(), 'local converter output')

    def test_local_dashboard_only_does_not_upload_or_restart(self):
        _, script = self.make_local_source()
        result, log = self.run_script('--source', 'local', '--dashboard-only', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('github.com' in line or line.startswith('scp ') for line in log))
        self.assertFalse(any('ha core restart' in line for line in log))
        self.assertTrue(any('--remote-backup-dir' in line and '--check' not in line and '/source/local/' in line for line in log))

    def test_local_snapshot_is_used_even_if_worktree_changes_during_deployment(self):
        root, script = self.make_local_source()
        self.env['MUTATE_LOCAL_FILE'] = str(root / 'custom_components/crestron/climate.py')
        result, _ = self.run_script('--source', 'local', '--update', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'capture/component/climate.py').read_text(), '# local unsaved changes')
        self.assertEqual(Path(self.env['MUTATE_LOCAL_FILE']).read_text(), 'changed after staging')

    def test_staging_under_local_root_does_not_copy_itself(self):
        root, script = self.make_local_source()
        self.env['TMPDIR'] = str(root)
        result, _ = self.run_script('--source', 'local', '--update', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.directory / 'capture/source').exists())
        self.assertFalse(list(root.glob('crestron-ha-deploy.*')))

    def test_local_missing_required_file_fails_before_remote_access(self):
        root, script = self.make_local_source()
        (root / 'custom_components/crestron/manifest.json').unlink()
        result, log = self.run_script('--source', 'local', '--update', script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('本地项目缺少', result.stderr)
        self.assertEqual(log, [])

    def test_local_symlink_fails_before_remote_access(self):
        root, script = self.make_local_source()
        (root / 'external-link').symlink_to(self.archive)
        result, log = self.run_script('--source', 'local', '--update', script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('链接或特殊文件', result.stderr)
        self.assertEqual(log, [])

    def test_invalid_source_and_local_branch_conflicts_fail_before_access(self):
        for args in [('--source', 'other'), ('--source', 'local', '--branch', 'dev'),
                     ('--branch', 'master', '--source', 'local')]:
            result, log = self.run_script(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log, [])

    def test_interactive_local_displays_inferred_directory(self):
        root, script = self.make_local_source()
        result, log = self.run_script(script=script, interactive=True,
                                     input_text='wrong\n1\nha.example\nroot\n\nn\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2. GitHub   : ', result.stdout)
        self.assertIn('1. 本地目录 : ' + str(root), result.stdout)
        self.assertFalse(any('github.com' in line for line in log))

    def test_interactive_github_lists_branches_and_defaults_to_master(self):
        result, log = self.run_script(interactive=True,
                                     input_text='2\n\nha.example\nroot\n\nn\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1. dev', result.stdout)
        self.assertIn('2. master', result.stdout)
        self.assertTrue(any('/git/ref/heads/master' in line for line in log))

    def test_interactive_github_paginates_all_branches(self):
        self.env['TEST_BRANCHES'] = json.dumps([f'branch-{i}' for i in range(100)] + ['feature/温控'])
        result, log = self.run_script(interactive=True,
                                     input_text='2\n0\n999\nabc\n101\nha.example\nroot\n\nn\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('101. feature/温控', result.stdout)
        self.assertTrue(any('per_page=100&page=2' in line for line in log))
        self.assertTrue(any('/heads/feature%2F%E6%B8%A9%E6%8E%A7' in line for line in log))

    def test_interactive_without_master_defaults_to_first_branch(self):
        self.env['TEST_BRANCHES'] = '["main", "dev"]'
        result, log = self.run_script(interactive=True,
                                     input_text='2\n\nha.example\nroot\n\nn\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/git/ref/heads/main' in line for line in log))

    def test_branch_list_failures_stop_before_remote_access(self):
        for settings in ({'FAIL_BRANCHES': '1'}, {'BAD_BRANCHES': '1'}, {'TEST_BRANCHES': '[]'},
                         {'TEST_BRANCHES': '["bad name"]'}):
            self.env.update(settings)
            result, log = self.run_script(interactive=True, input_text='2\n')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(line.startswith(('ssh ', 'scp ')) or '/tar.gz/' in line for line in log))
            for key in settings:
                self.env.pop(key)

    def test_interactive_eof_does_not_start_deployment(self):
        for answer in ('', '2\n'):
            result, log = self.run_script(interactive=True, input_text=answer)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(line.startswith(('ssh ', 'scp ')) or '/tar.gz/' in line for line in log))

    def test_explicit_github_branch_skips_interactive_listing(self):
        result, log = self.run_script('--source', 'github', '--branch', 'dev', '--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('/branches?' in line for line in log))
        self.assertTrue(any('/git/ref/heads/dev' in line for line in log))

    def test_dashboard_only_skips_component_yaml_check_and_restart(self):
        result, log = self.run_script('--dashboard-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(line.startswith('scp ') for line in log))
        self.assertFalse(any('ha core check' in line or 'ha core restart' in line for line in log))
        self.assertTrue(any('--remote-backup-dir' in line and '--check' not in line for line in log))
        self.assertNotIn('是否从 Excel', result.stdout)

    def test_regular_flow_checks_restarts_waits_then_deploys_dashboard(self):
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        check = next(i for i, line in enumerate(log) if 'ha core check' in line)
        dashboard = next(i for i, line in enumerate(log) if '--remote-backup-dir' in line and '--check' not in line)
        restart = next(i for i, line in enumerate(log) if 'ha core restart' in line)
        probe = next(i for i, line in enumerate(log) if line.startswith('remote-curl '))
        self.assertLess(check, restart)
        self.assertLess(restart, probe)
        self.assertLess(probe, dashboard)
        self.assertIn('--wait-seconds 120', log[dashboard])

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
        self.assertFalse(any('--remote-backup-dir' in line and '--check' not in line or 'ha core restart' in line for line in log))

    def test_failed_dashboard_after_restart_stops_second_stage(self):
        xlsx = self.automation_sources()
        self.env['FAIL_DASHBOARD'] = '1'
        result, log = self.run_script('--xlsx', str(xlsx), '--skip-generate-yaml',
                                      '--skip-upload-yaml', '--generate-automations', '--skip-upload-automations')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any('ha core restart' in line for line in log))
        self.assertFalse(any(line.startswith('automation-stage2') for line in log))

    def test_conflicting_flags_fail_before_remote_access(self):
        for flag in ['--skip-dashboard', '--generate-yaml', '--upload-yaml']:
            result, log = self.run_script('--dashboard-only', flag)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log, [])

    def test_default_install_downloads_master_and_only_uploads_runtime_files(self):
        result, log = self.run_script('--skip-generate-yaml', '--skip-upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/git/ref/heads/master' in line for line in log))
        self.assertTrue(any('/tar.gz/' + self.sha in line for line in log))
        capture = self.directory / 'capture'
        self.assertEqual((capture / 'component/climate.py').read_text(), '# downloaded climate\n')
        self.assertFalse((capture / 'component/crestron.yaml').exists())
        self.assertFalse((capture / 'component/__pycache__').exists())
        self.assertEqual({p.name for p in capture.iterdir()}, {'component'})
        self.assertFalse(any('crestron-source' in line for line in log))
        self.assertTrue(any('--check' in line and '/source/repo-' in line for line in log))
        self.assertTrue(any('--remote-backup-dir' in line and '--check' not in line and '/source/repo-' in line for line in log))
        self.assertTrue(any('http://ha.example:8123/api/' in line for line in log))
        self.assertFalse(any('ha host reboot' in line for line in log))

    @unittest.skipUnless(REAL_SCP, 'real scp is required for protocol regression')
    def test_real_scp_uploads_only_github_component_with_hidden_and_unicode_files(self):
        self.env['REAL_SCP'] = REAL_SCP
        self.make_archive(extra={'custom_components/crestron/.hidden': 'hidden',
                                 'custom_components/crestron/文档 with spaces/说明.md': 'documentation'})
        result, _ = self.run_script('--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        component = self.directory / 'capture/component'
        self.assertTrue((component / 'manifest.json').is_file())
        self.assertEqual((component / '.hidden').read_text(), 'hidden')
        self.assertEqual((component / '文档 with spaces/说明.md').read_text(), 'documentation')
        self.assertFalse((component / 'crestron').exists())
        self.assertFalse((self.directory / 'capture/source').exists())

    @unittest.skipUnless(REAL_SCP, 'real scp is required for protocol regression')
    def test_real_scp_uploads_local_component_and_keeps_yaml_separate(self):
        self.env['REAL_SCP'] = REAL_SCP
        root, script = self.make_local_source()
        (root / '.hidden file').write_text('hidden source')
        result, _ = self.run_script('--source', 'local', '--update', '--upload-yaml',
                                   '--yaml', str(root / 'custom_components/crestron/crestron.yaml'),
                                   script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        component = self.directory / 'capture/component'
        self.assertTrue((component / 'new_module.py').is_file())
        self.assertFalse((component / 'crestron.yaml').exists())
        self.assertEqual({p.name for p in (self.directory / 'capture').iterdir()}, {'component', 'yaml'})
        self.assertEqual((self.directory / 'capture/yaml').read_text(), 'do not deploy automatically')

    def test_update_defaults_preserve_yaml_and_requires_existing_component(self):
        result, log = self.run_script('--update', '--branch', 'dev')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('/git/ref/heads/dev' in line for line in log))
        self.assertTrue(any('test -f' in line and 'manifest.json' in line for line in log))
        self.assertFalse((self.directory / 'capture/yaml').exists())
        self.assertNotIn('是否从 Excel', result.stdout)

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

    def test_backup_archive_contains_old_yaml_and_shares_directory_with_dashboards(self):
        result, log = self.run_script('--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = list((self.directory / 'remote/crestron-backups').glob('*/*/crestron.tar.gz'))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        self.assertRegex(archive.parent.parent.name, r'^\d{4}-\d{2}-\d{2}$')
        self.assertRegex(archive.parent.name, r'^\d{6}-[A-Za-z0-9]+$')
        with tarfile.open(archive) as backup:
            self.assertEqual(backup.extractfile('crestron/crestron.yaml').read(), b'original site configuration')
            self.assertEqual(backup.extractfile('crestron/.hidden').read(), b'preserve hidden files')
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        remote_directory = str(archive.parent).replace(str(self.directory / 'remote'), '/homeassistant')
        self.assertTrue(any('--remote-backup-dir ' + remote_directory + '/dashboard' in line for line in log))
        backup_index = next(i for i, line in enumerate(log) if 'tar -czf' in line)
        upload_index = next(i for i, line in enumerate(log) if line.startswith('scp '))
        self.assertLess(backup_index, upload_index)
        self.assertFalse(list(archive.parent.glob('*.part')))
        self.assertFalse(list(self.directory.glob('crestron-ha-deploy.*')))

    def test_repeat_deployments_do_not_overwrite_backups(self):
        for _ in range(2):
            result, _ = self.run_script('--update', '--skip-dashboard')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list((self.directory / 'remote/crestron-backups').glob('*/*/crestron.tar.gz'))), 2)

    def test_first_install_has_no_old_component_archive(self):
        shutil.rmtree(self.directory / 'remote/custom_components/crestron')
        result, _ = self.run_script('--skip-dashboard', '--skip-generate-yaml', '--skip-upload-yaml')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('首次安装，无旧组件', result.stdout)
        self.assertFalse(list((self.directory / 'remote').rglob('*.tar.gz')))

    def test_backup_creation_and_validation_failures_stop_upload_and_remove_partial(self):
        for failure in ('FAIL_BACKUP_DIR', 'FAIL_TAR', 'FAIL_TAR_VERIFY'):
            with self.subTest(failure=failure):
                self.env[failure] = '1'
                result, log = self.run_script('--update')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(line.startswith('scp ') or 'ha core restart' in line for line in log))
                self.assertFalse(list((self.directory / 'remote').rglob('*.part')))
                self.assertFalse(list((self.directory / 'remote').rglob('*.tar.gz')))
                self.assertFalse(list(self.directory.glob('crestron-ha-deploy.*')))
                self.env.pop(failure)

    def test_component_upload_failure_stops_restart(self):
        self.env['FAIL_UPLOAD'] = 'component'
        result, log = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / 'capture/component').exists())
        self.assertFalse(any('ha core restart' in line for line in log))

    def test_restart_failure_does_not_report_success(self):
        self.env['FAIL_RESTART'] = '1'
        result, log = self.run_script('--update')
        self.assertFalse(any('--remote-backup-dir' in line and '--check' not in line for line in log))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('部署完成：', result.stdout)

    def test_http_probe_timeout_does_not_report_success(self):
        self.env['PROBE_STATUS'] = '503'
        result, log = self.run_script('--update', '--restart-timeout', '1')
        self.assertFalse(any('--remote-backup-dir' in line and '--check' not in line for line in log))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未在 1 秒内恢复', result.stderr)
        self.assertIn('HTTP 状态：503', result.stdout)
        self.assertIn('http://ha.example:8123/api/', result.stderr)
        self.assertNotIn('部署完成：', result.stdout)

    def test_recovery_uses_remote_curl_despite_local_proxy(self):
        self.env['http_proxy'] = 'http://unreachable.example:7890'
        self.env['PROBE_STATUS'] = '200'
        result, log = self.run_script('--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(line.startswith('ssh ') and 'curl -sS' in line for line in log))
        self.assertTrue(any(line.startswith('remote-curl ') and '/api/' in line for line in log))
        self.assertFalse(any(line.startswith('curl ') and '/api/' in line for line in log))
        self.assertIn('服务器端检查', result.stdout)

    def test_recovery_retries_until_api_is_ready_without_restarting_again(self):
        self.env['PROBE_STATUSES'] = '503,401'
        result, log = self.run_script('--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('HTTP 状态：503', result.stdout)
        self.assertEqual(sum('ha core restart' in line for line in log), 1)
        self.assertEqual(sum(line.startswith('remote-curl ') for line in log), 2)

    def test_remote_curl_required_before_upload(self):
        self.env['MISSING_REMOTE_CURL'] = '1'
        result, log = self.run_script('--update')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('服务器端缺少 curl', result.stderr)
        self.assertFalse(any(line.startswith('scp ') or 'ha core restart' in line for line in log))

    def test_remote_curl_failure_reports_error_even_with_success_status(self):
        self.env.update(PROBE_STATUS='401', MOCK_PROBE_EXIT='28', PROBE_ERROR='curl: request timed out')
        result, _ = self.run_script('--update', '--restart-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('检查退出码：28', result.stdout)
        self.assertIn('curl: request timed out', result.stderr)
        self.assertNotIn('部署完成：', result.stdout)

    def test_probe_ssh_failure_reports_error(self):
        self.env['FAIL_PROBE_SSH'] = '1'
        result, _ = self.run_script('--update', '--restart-timeout', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('检查退出码：1', result.stdout)
        self.assertIn('SSH connection lost', result.stderr)
        self.assertNotIn('部署完成：', result.stdout)

    def test_remote_probe_quotes_url_for_posix_shell(self):
        url = "https://ha.example/it's-$(printf injected)"
        self.env['EXPECTED_PROBE_URL'] = url + '/api/'
        result, _ = self.run_script('--update', '--skip-dashboard', '--ha-url', url)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_local_is_default_with_connection_arguments(self):
        root, script = self.make_local_source()
        result, log = self.run_script('--update', script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(line.startswith('curl ') for line in log))
        self.assertEqual((self.directory / 'capture/component/climate.py').read_text(), '# local unsaved changes')

    def test_interactive_enter_selects_local(self):
        root, script = self.make_local_source()
        result, log = self.run_script(script=script, interactive=True,
                                     input_text='\nha.example\nroot\n\nn\nn\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1. 本地目录 : ' + str(root), result.stdout)
        self.assertFalse(any(line.startswith('curl ') for line in log))

    def test_standalone_interactive_downloads_before_running_any_step(self):
        standalone = self.directory / 'single-file/deploy_to_ha.sh'
        standalone.parent.mkdir()
        shutil.copy2(ROOT / 'tools/deploy_to_ha.sh', standalone)
        result, log = self.run_script(script=standalone, interactive=True,
                                     input_text='\nha.example\nroot\n\nn\nn\nn\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('未检测到本地项目', result.stdout)
        download = next(i for i, line in enumerate(log) if '/tar.gz/' in line)
        ssh = next(i for i, line in enumerate(log) if line.startswith('ssh '))
        self.assertLess(download, ssh)
        self.assertEqual({p.name for p in standalone.parent.iterdir()}, {'deploy_to_ha.sh'})
        self.assertFalse(list(self.directory.glob('crestron-ha-deploy.*')))

    def test_missing_downloaded_deployment_tools_stops_before_ssh(self):
        for name in ('deploy_devices.py', 'deploy_common.py', 'deploy_dashboards.py'):
            with self.subTest(name=name):
                self.make_archive(omit='tools/' + name)
                result, log = self.run_script('--update')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('缺少 tools/' + name, result.stderr)
                self.assertFalse(any(line.startswith(('ssh ', 'scp ')) for line in log))
                self.assertFalse(list(self.directory.glob('crestron-ha-deploy.*')))

    def test_python_device_entry_can_run_local_independently(self):
        root, _ = self.make_local_source()
        result = subprocess.run([sys.executable, '-B', str(root / 'tools/deploy_devices.py'),
                                 '--host', 'ha.example', '--user', 'root', '--key', '--update'],
                                env=self.env, input='', text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertNotIn('github.com', log)
        self.assertIn('ha core restart', log)
        self.assertNotIn('automation-stage2', log)

    def test_python_device_entry_downloads_selected_github_source(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/deploy_devices.py'),
                                 '--source', 'github', '--branch', 'feature/设备',
                                 '--host', 'ha.example', '--user', 'root', '--key', '--update'],
                                env=self.env, input='', text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/git/ref/heads/feature%2F', self.log.read_text())
        self.assertEqual((self.directory / 'capture/component/climate.py').read_text(), '# downloaded climate\n')

    def test_python_dashboard_entry_downloads_source_without_device_or_automation_steps(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/deploy_dashboards.py'),
                                 '--source', 'github', '--branch', 'feature/dashboard',
                                 '--host', 'ha.example', '--user', 'root', '--key'],
                                env=self.env, input='', text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertIn('/git/ref/heads/feature%2Fdashboard', log)
        self.assertLess(log.index('/tar.gz/'), log.index('ssh '))
        self.assertIn('--remote-backup-dir', log)
        self.assertNotIn('ha core restart', log)
        self.assertNotIn('scp ', log)
        self.assertNotIn('automation-stage2', log)

    def test_python_dashboard_entry_without_arguments_prompts_and_defaults_to_local(self):
        root, _ = self.make_local_source()
        result = subprocess.run([sys.executable, '-B', str(root / 'tools/deploy_dashboards.py')],
                                env=self.env, input='\nha.example\n\n\n', text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertNotIn('github.com', log)
        self.assertNotIn('ha core restart', log)
        self.assertNotIn('是否从 Excel', result.stdout + result.stderr)
        self.assertIn('--remote-backup-dir', log)

    def test_device_only_never_runs_dashboard_backend(self):
        result, log = self.run_script('--devices-only', '--update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('deploy_dashboards' in line for line in log))
        self.assertTrue(any('ha core restart' in line for line in log))

    def test_only_flags_are_mutually_exclusive_before_download(self):
        result, log = self.run_script('--devices-only', '--dashboard-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(log, [])

    def test_python_automation_entry_downloads_and_only_runs_second_step(self):
        xlsx = self.automation_sources()
        output = self.directory / 'auto.yaml'
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'tools/deploy_automations.py'),
                                 '--source', 'github', '--host', 'ha.example', '--user', 'root', '--key',
                                 '--xlsx', str(xlsx), '--automation-yaml', str(output), '--skip-upload-automations'],
                                env=self.env, input='', text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertIn('/tar.gz/', log)
        self.assertIn('automation-stage2', log)
        self.assertNotIn('ha core restart', log)
        self.assertNotIn('scp ', log)
        self.assertEqual(output.read_text(), 'generated associations')

    def test_device_failure_prevents_second_step(self):
        xlsx = self.automation_sources()
        self.env['FAIL_CORE_CHECK'] = '1'
        result, log = self.run_script('--generate-automations', '--xlsx', str(xlsx),
                                      '--yaml', str(self.directory / 'devices.yaml'), '--skip-upload-yaml')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('automation-stage2' in line or 'ha core restart' in line for line in log))

    def test_device_yaml_is_generated_before_upload_prompt(self):
        xlsx = self.directory / 'site.xlsx'
        xlsx.write_text('fixture')
        output = self.directory / 'device.yaml'
        result, _ = self.run_script('--devices-only', '--xlsx', str(xlsx), '--yaml', str(output),
                                    input_text='y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index('正在生成 YAML'), result.stdout.index('是否上传'))
        self.assertEqual((self.directory / 'capture/yaml').read_text(), 'downloaded converter output')

    def test_device_upload_enter_defaults_to_yes(self):
        output = self.directory / 'device.yaml'
        output.write_text('local device configuration')
        result, log = self.run_script('--devices-only', '--skip-generate-yaml',
                                    '--yaml', str(output), input_text='\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[Y/n]', result.stdout)
        self.assertEqual((self.directory / 'capture/yaml').read_text(), 'local device configuration')
        self.assertNotIn('\033', result.stdout + result.stderr)
        self.assertTrue(all('-q' in line.split() for line in log if line.startswith('scp ')))
        self.assertIn('最后修改时间（UTC）', result.stdout)
        self.assertIn(' B ', result.stdout)

    def test_enter_generates_both_files_and_uploads_devices(self):
        xlsx = self.automation_sources()
        output = self.directory / 'device.yaml'
        automation = self.directory / 'automatic.yaml'
        result, log = self.run_script('--yaml', str(output),
                                      '--automation-yaml', str(automation),
                                      input_text=f'\n\n{xlsx}\n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count('[Y/n]'), 2)
        self.assertEqual(output.read_text(), 'downloaded converter output')
        self.assertEqual((self.directory / 'capture/yaml').read_text(), output.read_text())
        self.assertEqual(automation.read_text(), 'generated associations')
        stage = next(i for i, line in enumerate(log) if line.startswith('automation-stage2'))
        restart = next(i for i, line in enumerate(log) if 'ha core restart' in line)
        dashboard = next(i for i, line in enumerate(log)
                         if '--remote-backup-dir' in line and '--check' not in line)
        self.assertLess(restart, stage)
        self.assertLess(restart, dashboard)
        self.assertLess(dashboard, stage)
        self.assertNotIn('\033', result.stdout + result.stderr)

    def test_password_reaches_python_connection_without_entering_command_log(self):
        shim = self.directory / 'sshpass'
        shim.write_text('#!/bin/sh\n[ "$1" = -e ] || exit 1\n'
                        '[ "$SSHPASS" = "$HA_SSH_PASSWORD" ] || exit 2\n'
                        '[ -n "$SSHPASS" ] || exit 3\nshift\nexec "$@"\n')
        shim.chmod(0o755)
        secret = 'test password with spaces'
        result, log = self.run_script('--update', '--password', secret)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('PreferredAuthentications=password,keyboard-interactive' in line for line in log))
        self.assertNotIn(secret, '\n'.join(log) + result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
