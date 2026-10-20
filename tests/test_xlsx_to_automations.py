"""Excel 关联生成器的实际工作簿、注册表匹配和失败保护回归。"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import xlsx_to_automations as g


def workbook(path, sheet='灯光', rows=None):
    headers = ['楼层', '房间', '名称', '亮度', '开', '关', g.SWITCH_COLUMN, g.OCCUPANCY_COLUMN]
    rows = rows or [['B2', '车库', '灯带', '2', '', '', 'switch.button\nswitch.other', 'binary_sensor.presence']]
    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
    ws = ET.Element(ns + 'worksheet')
    data = ET.SubElement(ws, ns + 'sheetData')
    for line, cells in [(1, headers)] + [(12 + n, r) for n, r in enumerate(rows)]:
        row = ET.SubElement(data, ns + 'row', {'r': str(line)})
        for col, value in enumerate(cells):
            cell = ET.SubElement(row, ns + 'c', {'r': f'{chr(65 + col)}{line}', 't': 'inlineStr'})
            ET.SubElement(ET.SubElement(cell, ns + 'is'), ns + 't').text = value
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('xl/workbook.xml', f'<workbook xmlns="{ns[1:-1]}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{sheet}" r:id="r1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', ET.tostring(ws))


def registry():
    return dict(entities=[
        dict(entity_id='light.renamed', platform='crestron', unique_id='crestron_light_2', name='自定义灯', device_id='lamp'),
        dict(entity_id='switch.button', platform='any_brand', unique_id='button', device_id='button', original_name='左键', has_entity_name=True),
        dict(entity_id='switch.other', platform='another_brand', name='另一开关', area_id='room'),
        dict(entity_id='binary_sensor.presence', platform='any_brand', name='人在传感器')],
        devices=[dict(id='lamp', area_id='garage'), dict(id='button', name='床头面板', area_id='room')],
        areas=[dict(id='room', name='卧室', floor_id='f1'), dict(id='garage', name='车库', floor_id='b2')],
        floors=[dict(id='f1', name='1F'), dict(id='b2', name='B2')])


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.xlsx = self.root / '设备.xlsx'
        self.output = self.root / 'automations_manual.yaml'
        workbook(self.xlsx)

    def test_actual_workbook_sparse_rows_multiline_renamed_and_readable_report(self):
        rows = g.read_associations(self.xlsx)
        self.assertEqual(rows[0]['location'], '灯光表第12行')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            content = g.generate(self.xlsx, registry(), self.output)
        doc = yaml.safe_load(content)
        maps = doc['actions'][0]['variables']
        self.assertEqual(maps['switch_map']['switch.button']['light_entity_ids'], ['light.renamed'])
        self.assertEqual(maps['occupancy_map']['binary_sensor.presence']['light_entity_ids'], ['light.renamed'])
        self.assertEqual(doc['id'], 'crestron_manual_associations')
        self.assertIn('B2 / 车库 / 灯带 [light.renamed]', out.getvalue())
        self.assertIn('1F / 卧室 / 床头面板 左键 [switch.button]', content)
        self.assertIn('未分配楼层 / 未分配房间 / 人在传感器', content)
        self.assertNotIn('@@', content)
        self.assertIn('{{', content)
        self.assertEqual(self.output.read_text(), content)

    def test_actual_and_literal_newlines_deduplicate(self):
        self.assertEqual(g.split_ids(' switch.a\r\nswitch.b\\nswitch.a\n\n', 'switch', '位置'), ['switch.a', 'switch.b'])
        with self.assertRaisesRegex(g.AssociationError, '每行一项'):
            g.split_ids('switch.a,switch.b', 'switch', '位置')

    def test_multi_device_mapping_merges(self):
        workbook(self.xlsx, rows=[['B2','车库','灯一','2','','','switch.button',''],
                                 ['B2','车库','灯二','3','','','switch.button','']])
        snapshot = registry()
        snapshot['entities'].append(dict(entity_id='light.two', platform='crestron', unique_id='crestron_light_3'))
        sm, om, report = g.build_maps(g.read_associations(self.xlsx), snapshot)
        self.assertEqual(sm['switch.button']['light_entity_ids'], ['light.renamed','light.two'])
        self.assertFalse(om)
        self.assertIn('关联 2 台设备', '\n'.join(report))

    def test_missing_disabled_and_ambiguous_entities_leave_old_output(self):
        self.output.write_text('旧配置')
        for mode in ['missing', 'disabled', 'duplicate']:
            with self.subTest(mode=mode):
                snapshot = registry()
                if mode == 'missing':
                    snapshot['entities'].pop(0)
                elif mode == 'disabled':
                    snapshot['entities'][0]['disabled_by'] = 'user'
                else:
                    snapshot['entities'].append(dict(entity_id='light.duplicate', platform='crestron', unique_id='crestron_light_2'))
                with self.assertRaises(g.AssociationError):
                    g.generate(self.xlsx, snapshot, self.output)
                self.assertEqual(self.output.read_text(), '旧配置')

    def test_registered_but_not_loaded_retries(self):
        snapshot = registry()
        snapshot['states'] = []
        with self.assertRaisesRegex(g.RegistryPending, '尚未加载'):
            g.build_maps(g.read_associations(self.xlsx), snapshot)

    def test_duplicate_device_identity_reports_excel_row(self):
        workbook(self.xlsx, rows=[['B2','车库','灯一','2','','','switch.button',''],
                                 ['B2','车库','灯二','2','','','','']])
        with self.assertRaisesRegex(g.AssociationError, '第12行.*重复'):
            g.read_associations(self.xlsx)

    def test_climate_identity_and_occupancy_rejection(self):
        workbook(self.xlsx, sheet='空调', rows=[['B2','车库','空调','','80','81','switch.button','']])
        rows = g.read_associations(self.xlsx)
        self.assertEqual(rows[0]['unique_id'], 'crestron_climate_d80')
        snapshot = registry()
        snapshot['entities'].append(dict(entity_id='climate.renamed', platform='crestron', unique_id='crestron_climate_d80'))
        sm, _, _ = g.build_maps(rows, snapshot)
        self.assertEqual(sm['switch.button']['climate_entity_ids'], ['climate.renamed'])
        workbook(self.xlsx, sheet='空调', rows=[['B2','车库','空调','','80','81','','binary_sensor.presence']])
        with self.assertRaisesRegex(g.AssociationError, '暂不支持人在'):
            g.read_associations(self.xlsx)

    def test_outlet_and_feedback_cycle_rejected(self):
        workbook(self.xlsx, sheet='插座', rows=[['B2','车库','插座','','10','11','switch.button','binary_sensor.presence']])
        rows = g.read_associations(self.xlsx)
        self.assertEqual(rows[0]['unique_id'], 'crestron_switch_d10')
        snapshot = registry()
        snapshot['entities'].append(dict(entity_id='switch.socket', platform='crestron', unique_id='crestron_switch_d10'))
        sm, om, _ = g.build_maps(rows, snapshot)
        self.assertEqual(sm['switch.button']['switch_entity_ids'], ['switch.socket'])
        self.assertEqual(om['binary_sensor.presence']['switch_entity_ids'], ['switch.socket'])
        rows[0]['switches'] = ['switch.socket']
        with self.assertRaisesRegex(g.AssociationError, '反馈循环'):
            g.build_maps(rows, snapshot)

    def test_template_validation_and_check_do_not_write(self):
        with contextlib.redirect_stdout(io.StringIO()):
            g.generate(self.xlsx, registry(), self.output, check=True)
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(g.AssociationError, '标记'):
            g.render_template('bad', {}, {}, [])
        with self.assertRaisesRegex(g.AssociationError, '不能覆盖'):
            g.generate(self.xlsx, registry(), g.DEFAULT_TEMPLATE)

    def test_wrong_source_domain_and_template_row_report_error(self):
        for source in ['light.a', 'switch.a\ninvalid']:
            workbook(self.xlsx, rows=[['B2','车库','灯','2','','',source,'']])
            with self.assertRaisesRegex(g.AssociationError, '第12行'):
                g.read_associations(self.xlsx)
        workbook(self.xlsx, rows=[['B2','车库','灯','','','','switch.button','']])
        with self.assertRaises(g.AssociationError):
            g.read_associations(self.xlsx)

    def test_no_associations_is_valid_empty_automation(self):
        workbook(self.xlsx, rows=[['B2','车库','灯','2','','','','']])
        with contextlib.redirect_stdout(io.StringIO()):
            content = g.generate(self.xlsx, {'entities': []}, self.output)
        self.assertEqual(yaml.safe_load(content)['triggers'], [])
        self.assertIn('移除该文件中的旧关联', content)

    def test_entity_area_override_precedes_device_area(self):
        snapshot = registry()
        snapshot['entities'][1]['area_id'] = 'garage'
        self.assertIn('B2 / 车库', g.Registry(snapshot).label('switch.button'))

    def test_devices_converter_does_not_emit_association_columns(self):
        with contextlib.redirect_stdout(io.StringIO()):
            g.devices.generate(str(self.xlsx), str(self.root / 'crestron.yaml'))
        content = (self.root / 'crestron.yaml').read_text()
        self.assertNotIn('switch.button', content)
        self.assertNotIn('无法识别', content)

    def test_dimmable_power_joins_survive_generation_and_match_existing_entity(self):
        workbook(self.xlsx, rows=[
            ['B2', '车库', '灯带', '2', '30', '31', 'switch.button', ''],
        ])
        device_output = self.root / 'crestron.yaml'
        with contextlib.redirect_stdout(io.StringIO()):
            g.devices.generate(str(self.xlsx), str(device_output))
        ent = yaml.safe_load(device_output.read_text())['light'][0]
        self.assertEqual((ent['brightness_join'], ent['on_join'], ent['off_join']),
                         (2, 30, 31))
        self.assertEqual(ent['device_id'], 'light_2')
        sm, _, _ = g.build_maps(g.read_associations(self.xlsx), registry())
        self.assertEqual(sm['switch.button']['light_entity_ids'], ['light.renamed'])

    def test_identity_matches_integration_contract(self):
        from loader import load
        uid = load('unique_ids')
        for platform, config in [('light', {'brightness_join': 2}),
                                 ('light', {'brightness_join': 2, 'on_join': 30, 'off_join': 31}),
                                 ('light', {'on_join': 3}),
                                 ('switch', {'on_join': 4}),
                                 ('climate', {'on_join': 5})]:
            self.assertEqual(g.identity(platform, config),
                             getattr(uid, platform + '_unique_id')(config))

    def test_raw_floor_registry_uses_floor_id(self):
        snapshot = registry()
        snapshot['floors'] = [{'floor_id': 'f1', 'name': '一层'}]
        self.assertIn('一层 / 卧室', g.Registry(snapshot).label('switch.button'))


if __name__ == '__main__':
    unittest.main()
