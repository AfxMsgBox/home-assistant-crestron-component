"""只读实体查询：匹配、位置继承、终端排版及认证参数。"""

import io
import os
from pathlib import Path
import re
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import list_ha_entities as tool


def snapshot():
    return {
        'entities': [
            {'entity_id': 'switch.kitchen_left', 'device_id': 'abc123-device-full-id',
             'name': '左键', 'area_id': 'kitchen', 'disabled_by': 'user'},
            {'entity_id': 'binary_sensor.study_occupancy', 'device_id': 'sensor-device',
             'original_name': '有人', 'has_entity_name': True},
            {'entity_id': 'light.unrelated', 'name': 'switch occ 只在名称里'},
            {'entity_id': 'switch.orphan', 'device_id': 'missing-device'},
        ],
        'devices': [
            {'id': 'abc123-device-full-id', 'name': '旧名称', 'name_by_user': '厨房开关', 'area_id': 'study'},
            {'id': 'sensor-device', 'name': '人在传感器', 'area_id': 'study'},
        ],
        'areas': [{'id': 'kitchen', 'name': '厨房', 'floor_id': 'floor1'},
                  {'id': 'study', 'name': '书房', 'floor_id': 'floor2'}],
        'floors': [{'id': 'floor1', 'name': '1F'}, {'id': 'floor2', 'name': '2F'}],
        'states': [{'entity_id': 'switch.kitchen_left', 'attributes': {'friendly_name': '旧左键'}},
                   {'entity_id': 'binary_sensor.template_occupancy', 'attributes': {'friendly_name': '模板人在'}}],
    }


class EntityQueryTests(unittest.TestCase):
    def test_or_filter_uses_only_id_and_merges_live_entities(self):
        rows = tool.entity_rows(snapshot(), ['SWITCH', 'occ'])
        self.assertEqual({row[4] for row in rows}, {'switch.kitchen_left', 'switch.orphan',
                         'binary_sensor.study_occupancy', 'binary_sensor.template_occupancy'})
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(tool.entity_rows(snapshot(), [])), 5)
        self.assertEqual(tool.entity_rows(snapshot(), ['switch.*']), [])

    def test_location_override_names_and_missing_device(self):
        rows = {row[4]: row for row in tool.entity_rows(snapshot(), [])}
        self.assertEqual(rows['switch.kitchen_left'], ('1F', '厨房', '厨房开关', '左键',
                                                      'switch.kitchen_left', 'abc123-device-full-id'))
        self.assertEqual(rows['binary_sensor.study_occupancy'][:4], ('2F', '书房', '人在传感器', '有人'))
        self.assertEqual(rows['switch.orphan'][2], '设备信息缺失')
        self.assertEqual(rows['switch.orphan'][5], 'missing-device')
        self.assertEqual(rows['binary_sensor.template_occupancy'][:4], ('未分配', '未分配', '无关联设备', '模板人在'))

    def test_long_ids_chinese_and_combining_marks_align_without_truncation(self):
        rows = tool.entity_rows(snapshot(), [])
        long_id = 'switch.' + 'a' * 140
        rows.append(('B2', '车库', 'Cafe\u0301', '天花线条灯带', long_id, 'd' * 32))
        plain, colored = io.StringIO(), io.StringIO()
        tool.render(rows, ['switch', 'occ'], stream=plain)
        tool.render(rows, ['switch', 'occ'], stream=colored, color='always')
        output = plain.getvalue()
        self.assertNotIn('\033', output)
        self.assertIn(long_id, output)
        self.assertIn('d' * 32, output)
        self.assertIn('\033[', colored.getvalue())
        self.assertEqual(re.sub(r'\x1b\[[0-9;]*m', '', colored.getvalue()), output)
        table_lines = [line for line in output.splitlines() if line.startswith(('│', '┌', '├', '└'))]
        self.assertEqual(len({tool.display_width(line) for line in table_lines}), 1)
        positions = []
        for line in table_lines:
            if line.startswith('│'):
                positions.append([tool.display_width(line[:i]) for i, char in enumerate(line) if char == '│'])
        self.assertTrue(all(value == positions[0] for value in positions))

    def test_auto_color_respects_tty_no_color_and_dumb_terminal(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=True):
            self.assertTrue(tool.use_color('auto', stream))
            os.environ['NO_COLOR'] = ''
            self.assertFalse(tool.use_color('auto', stream))
            self.assertTrue(tool.use_color('always', stream))
        with patch.dict(os.environ, {'TERM': 'dumb'}, clear=True):
            self.assertFalse(tool.use_color('auto', stream))

    def test_control_characters_and_tsv_output(self):
        rows = [('1F', '厨\n房', '开关\x1b[31m', '左\t键', 'switch.foo', '完整-device-id')]
        output = io.StringIO()
        tool.render(rows, ['switch'], stream=output, color='always', format='tsv')
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(lines[1].split('\t')), 6)
        self.assertNotIn('\033', output.getvalue())
        self.assertIn(r'厨\n房', output.getvalue())
        self.assertIn('完整-device-id', output.getvalue())

    def test_default_key_ignores_password_environment_and_preserves_ssh_defaults(self):
        args = tool.parse_args(['--host', 'ha-alias', 'occ'])
        with patch.dict(os.environ, HA_SSH_PASSWORD='previous', SSHPASS='previous'), \
             patch.object(tool.common, 'require'):
            with tool.password_environment(args.password):
                connection = tool.common.Connection(args)
            self.assertEqual(os.environ['HA_SSH_PASSWORD'], 'previous')
            self.assertNotIn('sshpass', connection.ssh)
            self.assertNotIn('-i', connection.ssh)
            self.assertNotIn('-F', connection.ssh)
            self.assertIn('BatchMode=yes', connection.ssh)

    def test_password_is_only_forwarded_via_environment(self):
        args = tool.parse_args(['--host', 'ha', '--password', 'secret value', 'switch'])
        with tool.password_environment(args.password), patch.object(tool.common, 'require'):
            connection = tool.common.Connection(args)
        self.assertEqual(connection.ssh[:2], ['sshpass', '-e'])
        self.assertEqual(connection.env['SSHPASS'], 'secret value')
        self.assertNotIn('secret value', str(connection.ssh))

    def test_interactive_prompts_defaults_and_expands_identity(self):
        with patch('builtins.input', side_effect=['ha', '', 'bad', '', '~/.ssh/custom', 'switch occ']), \
             patch.object(tool.getpass, 'getpass', return_value=''), patch('sys.stderr', new=io.StringIO()):
            args = tool.parse_args([])
        self.assertEqual((args.host, args.user, args.port), ('ha', 'root', 22))
        self.assertEqual(args.identity, str(Path('~/.ssh/custom').expanduser()))
        self.assertEqual(args.keywords, ['switch', 'occ'])

    def test_interactive_password_does_not_prompt_for_identity(self):
        with patch('builtins.input', side_effect=['ha', '', '', 'switch occ']), \
             patch.object(tool.getpass, 'getpass', return_value='test-secret'):
            args = tool.parse_args([])
        self.assertEqual(args.password, 'test-secret')
        self.assertIsNone(args.identity)

    def test_fetch_only_calls_read_api_and_registry(self):
        args = tool.parse_args(['--host', 'ha', '--password', 'test-secret'])
        with patch.object(tool.common, 'Connection') as connection, \
             patch.object(tool.common, 'core_url', return_value='http://ha'), \
             patch.object(tool.automation, 'Api') as api, \
             patch.object(tool.automation, 'read_registry', return_value=snapshot()) as read, \
             patch.object(tool.automation, 'upload') as upload, \
             patch.object(tool.common, 'create_backup') as backup:
            connection.return_value.ssh = ['ssh', 'server']
            self.assertEqual(tool.fetch_snapshot(args), snapshot())
        read.assert_called_once_with(['ssh', 'server'], api.return_value)
        upload.assert_not_called()
        backup.assert_not_called()

    def test_eof_and_bad_arguments_do_not_connect(self):
        with patch('builtins.input', side_effect=EOFError), \
             patch.object(tool, 'fetch_snapshot') as fetch, patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(tool.main([]), 130)
        fetch.assert_not_called()
        for argv in [['switch'], ['--host', 'ha', '--port', '0'], ['--host', 'ha', '']]:
            with self.subTest(argv=argv), patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
                tool.parse_args(argv)

    def test_main_stdout_contains_only_results_and_no_match_is_success(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(tool, 'fetch_snapshot', return_value=snapshot()), \
             patch('sys.stdout', new=output), patch('sys.stderr', new=errors):
            self.assertEqual(tool.main(['--host', 'ha', 'not-found']), 0)
        self.assertIn('没有匹配的实体', output.getvalue())
        self.assertNotIn('正在只读', output.getvalue())

    def test_default_filters_and_explicit_all(self):
        self.assertEqual(tool.parse_args(['--host', 'ha']).keywords, list(tool.DEFAULT_KEYWORDS))
        self.assertEqual(tool.parse_args(['--host', 'ha', '*']).keywords, [])
        self.assertEqual(tool.parse_args(['--host', 'ha', 'OCC', 'occ']).keywords, ['occ'])
        # 星号只有单独输入时特殊，其余包含星号的词仍按字面匹配。
        self.assertEqual(tool.query_keywords(['switch.*']), ['switch.*'])

    def test_display_name_deduplication_does_not_change_source_or_ids(self):
        data = snapshot()
        original_id = data['entities'][0]['entity_id']
        for name, expected in [('厨房开关 左键', '左键'), ('厨房开关：左键', '左键'),
                               ('厨房开关', '—'), ('左键（自定义）', '左键（自定义）')]:
            data['entities'][0]['name'] = name
            row = next(row for row in tool.entity_rows(data, []) if row[4] == original_id)
            self.assertEqual(row[2:4], ('厨房开关', expected))
            self.assertEqual(row[4:], (original_id, 'abc123-device-full-id'))
            self.assertEqual(data['entities'][0]['name'], name)

    def test_interactive_requeries_defaults_custom_all_then_ctrl_c(self):
        seen = []
        def fetch(args):
            seen.append(list(args.keywords))
            return snapshot()
        with patch('builtins.input', side_effect=['ha', '', '', '', '', 'switch occ', '*', '', KeyboardInterrupt]), \
             patch.object(tool.getpass, 'getpass', return_value=''), \
             patch.object(tool, 'fetch_snapshot', side_effect=fetch), \
             patch('sys.stdout', new=io.StringIO()), patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(tool.main([]), 130)
        self.assertEqual(seen, [list(tool.DEFAULT_KEYWORDS), ['switch', 'occ'], [], list(tool.DEFAULT_KEYWORDS)])

    def test_interactive_query_error_allows_retry(self):
        with patch('builtins.input', side_effect=['ha', '', '', '', '', 'switch', KeyboardInterrupt]), \
             patch.object(tool.getpass, 'getpass', return_value=''), \
             patch.object(tool, 'fetch_snapshot', side_effect=[tool.common.DeployError('连接失败'), snapshot()]) as fetch, \
             patch('sys.stdout', new=io.StringIO()), patch('sys.stderr', new=io.StringIO()) as errors:
            self.assertEqual(tool.main([]), 130)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('连接失败', errors.getvalue())

    def test_wide_tty_uses_nonwrapping_pager_with_full_aligned_rows(self):
        args = tool.parse_args(['--host', 'ha', 'occ', '--color', 'always'])
        output = io.StringIO()
        output.isatty = lambda: True
        rows = tool.entity_rows(snapshot(), [])
        with patch('sys.stdout', new=output), patch('sys.stderr', new=io.StringIO()), \
             patch.object(tool.shutil, 'get_terminal_size', return_value=os.terminal_size((60, 24))), \
             patch.object(tool.shutil, 'which', return_value='/usr/bin/less'), \
             patch.dict(os.environ, LESS='-F', LESSOPEN='unwanted', LESSCLOSE='unwanted'), \
             patch.object(tool.subprocess, 'Popen') as popen:
            pager = popen.return_value.__enter__.return_value
            pager.returncode = 0
            tool.show_results(rows, args)
        self.assertEqual(popen.call_args.args[0], ['/usr/bin/less', '-S', '-R', '-K'])
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['LESS'], '')
        self.assertEqual(env['LESSSECURE'], '1')
        self.assertNotIn('LESSOPEN', env)
        self.assertNotIn('LESSCLOSE', env)
        text = pager.communicate.call_args.args[0]
        plain = re.sub(r'\x1b\[[0-9;]*m', '', text)
        lines = [line for line in plain.splitlines() if line.startswith('│')]
        self.assertEqual(len(lines), len(rows) + 1)
        for row in rows:
            self.assertTrue(any(row[4] in line and row[5] in line for line in lines))
        self.assertEqual(len({tool.display_width(line) for line in lines}), 1)
        self.assertEqual(output.getvalue(), '')

    def test_redirect_and_tsv_never_start_pager(self):
        for format in ('table', 'tsv'):
            args = tool.parse_args(['--host', 'ha', '--format', format])
            with patch('sys.stdout', new=io.StringIO()) as output, patch.object(tool.subprocess, 'Popen') as pager:
                tool.show_results(tool.entity_rows(snapshot(), []), args)
            pager.assert_not_called()
            self.assertIn('abc123-device-full-id', output.getvalue())

    def test_pager_ctrl_c_exits_instead_of_requerying(self):
        args = tool.parse_args(['--host', 'ha'])
        output = io.StringIO()
        output.isatty = lambda: True
        with patch('sys.stdout', new=output), patch('sys.stderr', new=io.StringIO()), \
             patch.object(tool.shutil, 'get_terminal_size', return_value=os.terminal_size((60, 24))), \
             patch.object(tool.shutil, 'which', return_value='/usr/bin/less'), \
             patch.object(tool.subprocess, 'Popen') as popen:
            popen.return_value.__enter__.return_value.returncode = 2
            with self.assertRaises(KeyboardInterrupt):
                tool.show_results(tool.entity_rows(snapshot(), []), args)

    def test_missing_pager_reports_error_without_printing_wrapped_table(self):
        args = tool.parse_args(['--host', 'ha'])
        output = io.StringIO()
        output.isatty = lambda: True
        with patch('sys.stdout', new=output), \
             patch.object(tool.shutil, 'get_terminal_size', return_value=os.terminal_size((60, 24))), \
             patch.object(tool.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(tool.common.DeployError, 'less'):
                tool.show_results(tool.entity_rows(snapshot(), []), args)
        self.assertEqual(output.getvalue(), '')

    @unittest.skipUnless(os.name == 'posix' and shutil.which('less'), '需要本机 less 和伪终端')
    def test_real_pager_q_and_ctrl_c_restore_terminal(self):
        import errno
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios
        import time
        code = f'''import os, sys
sys.path.insert(0, {str(Path(tool.__file__).parent)!r})
import list_ha_entities as t
t.fetch_snapshot = lambda args: {{'entities': [{{'entity_id': 'switch.demo_oh4w_on_p_' + 'a' * 100}}]}}
result = t.main(['--host', 'demo'])
print('PAGER_RESULT=' + str(result), flush=True)
sys.exit(result)
'''
        for key, expected in [(b'q', 0), (b'\x03', 130)]:
            with self.subTest(key=key):
                pid, fd = pty.fork()
                if pid == 0:
                    fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 60, 0, 0))
                    os.environ.update(TERM='xterm-256color', LC_ALL='C.UTF-8')
                    os.execv(sys.executable, [sys.executable, '-B', '-c', code])
                output = b''
                sent = False
                reaped = False
                try:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if not select.select([fd], [], [], 0.1)[0]:
                            continue
                        try:
                            chunk = os.read(fd, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        output += chunk
                        if not sent and b'(END)' in output:
                            os.write(fd, key)
                            sent = True
                    self.assertTrue(sent, output.decode(errors='replace'))
                    self.assertIn(f'PAGER_RESULT={expected}'.encode(), output)
                    self.assertIn(b'\x1b[?1049l', output)  # less 已退出备用屏幕。
                    _, status = os.waitpid(pid, 0)
                    reaped = True
                    self.assertEqual(os.waitstatus_to_exitcode(status), expected)
                finally:
                    os.close(fd)
                    if not reaped:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        os.waitpid(pid, 0)


if __name__ == '__main__':
    unittest.main()
