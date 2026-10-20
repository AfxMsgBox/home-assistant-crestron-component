"""部署终端输出、中文对齐与默认覆盖选择；不连接服务器。"""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import deploy_common as common


class Tty(io.StringIO):
    def isatty(self):
        return True


class DeployConsoleTests(unittest.TestCase):
    def test_upload_file_table_uses_bytes_mtime_and_aligned_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / '中文.yaml', root / 'a.py'
            first.write_bytes('中文'.encode())
            second.write_bytes(b'x' * 2048)
            for path in (first, second):
                os.utime(path, (1704067200, 1704067200))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                common.local_files([first, second], base=root)
            text = output.getvalue()
            self.assertIn('6 B', text)
            self.assertIn('2,048 B', text)
            self.assertNotIn('\033', text)
            lines = text.splitlines()[2:]
            self.assertEqual(len(lines), 2)
            self.assertEqual(common.display_width(lines[0].split('2024-01-01')[0]),
                             common.display_width(lines[1].split('2024-01-01')[0]))
            self.assertIn('2024-01-01 00:00:00', text)

    def test_table_escapes_control_characters_without_shortening_ids(self):
        output = io.StringIO()
        eid = 'light.' + 'long_name_' * 20
        with contextlib.redirect_stdout(output):
            common.table(('名称', '实体 ID'), [('灯\n带\033', eid)])
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn(eid, output.getvalue())
        self.assertIn(r'灯\n带\x1b', output.getvalue())

    def test_terminal_color_and_no_color_override(self):
        with patch.dict(os.environ, {'TERM': 'xterm-256color'}, clear=True):
            self.assertIn('\033[32m', common.styled('完成', '32', Tty()))
            self.assertEqual(common.styled('完成', '32', io.StringIO()), '完成')
            with patch.dict(os.environ, {'NO_COLOR': ''}):
                self.assertEqual(common.styled('完成', '32', Tty()), '完成')
            with patch.dict(os.environ, {'TERM': 'dumb'}):
                self.assertEqual(common.styled('完成', '32', Tty()), '完成')

    def test_chinese_and_ascii_keys_align_without_color_in_logs(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            common.details([('设备配置', 'config.yaml'), ('SSH', 'root@example')])
            common.message('部署完成', 'success')
        lines = output.getvalue().splitlines()
        self.assertEqual(common.display_width(lines[0].split(' : ')[0]),
                         common.display_width(lines[1].split(' : ')[0]))
        self.assertNotIn('\033', output.getvalue())
        self.assertIn('[完成] 部署完成', lines[-1])

    def test_default_yes_needs_a_real_enter_and_explicit_no_still_wins(self):
        self.assertTrue(common.ask_yes_no('覆盖？', default=True, read=lambda _: ''))
        self.assertFalse(common.ask_yes_no('覆盖？', default=True, read=lambda _: 'n'))

        def eof(_):
            raise EOFError

        self.assertFalse(common.ask_yes_no('覆盖？', default=True, read=eof))
        # 清理注册表等非部署调用仍保留原有的默认 No。
        self.assertFalse(common.ask_yes_no('清理？', read=lambda _: ''))

    def test_invalid_answer_repeats_without_changing_default(self):
        replies = iter(['maybe', ''])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(common.ask_yes_no('覆盖？', default=True, read=lambda _: next(replies)))

    def test_shell_prompts_do_not_pollute_machine_readable_results(self):
        source = (ROOT / 'tools/deploy_to_ha.sh').read_text()
        ui = source[source.index('ui_log() {'):source.index('SCRIPT_DIR=')]
        ask = source[source.index('ask_yes_no() {'):source.index('\nfetch_source()')]
        for reply, expected in [('\n', 'yes'), ('n\n', 'no'), ('', 'no')]:
            result = subprocess.run(['bash', '-c', ui + ask + '\nask_yes_no "覆盖？"'],
                                    input=reply, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, expected)
            self.assertIn('[Y/n]', result.stderr)
            self.assertNotIn('\033', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
