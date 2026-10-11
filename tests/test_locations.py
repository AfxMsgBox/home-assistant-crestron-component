"""Registry-level location tests; no HA server or physical devices are used."""
import copy
from dataclasses import dataclass, replace
import sys
import types
import unittest
from unittest.mock import patch

from loader import load

locations = load("locations")


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    floor_id: str | None = None


class Areas:
    def __init__(self):
        self.items = {}
        self.events = []
        self.sequence = 0

    def async_list_areas(self):
        return list(self.items.values())

    def async_get_area(self, area_id):
        return self.items.get(area_id)

    def async_get_area_by_name(self, name):
        return next((a for a in self.items.values()
                     if locations.normalize_name(a.name) == locations.normalize_name(name)), None)

    def async_create(self, name, *, floor_id=None):
        if self.async_get_area_by_name(name):
            raise ValueError("Area names are globally unique")
        self.sequence += 1
        area = Area(f"area-{self.sequence}", name, floor_id)
        self.items[area.id] = area
        self.events.append(("create", area))
        return area

    def async_update(self, area_id, **changes):
        old = self.items[area_id]
        if "name" in changes:
            match = self.async_get_area_by_name(changes["name"])
            if match and match.id != area_id:
                raise ValueError("Area name already in use")
        area = replace(old, **changes)
        self.items[area_id] = area
        self.events.append(("update", area))
        return area


class Floors:
    def __init__(self):
        self.items = {}

    def async_get_floor_by_name(self, name):
        return self.items.get(locations.normalize_name(name))

    def async_create(self, name):
        key = locations.normalize_name(name)
        if key in self.items:
            raise ValueError("Duplicate floor")
        floor = types.SimpleNamespace(floor_id=f"floor-{len(self.items) + 1}", name=name)
        self.items[key] = floor
        return floor


def cfg(floor="1F", room="厨房", name="灯", device_id="light_1"):
    return dict(floor=floor, room=room, name=name, device_id=device_id)


class AreaSyncTests(unittest.TestCase):
    def setUp(self):
        self.floors, self.areas = Floors(), Areas()

    def sync(self, configs, saved=None):
        return locations.sync_areas(configs, self.floors, self.areas, saved or {})

    def area(self, mapping, floor="1F", room="厨房"):
        return self.areas.async_get_area(mapping[locations.location_key(floor, room)])

    def test_multiple_devices_share_one_floor_and_room(self):
        mapping = self.sync([cfg(), cfg(name="空调", device_id="ac_2")])
        self.assertEqual(len(self.floors.items), 1)
        self.assertEqual(len(self.areas.items), 1)
        self.assertEqual(self.area(mapping).name, "厨房")
        self.assertEqual(self.area(mapping).floor_id, self.floors.async_get_floor_by_name("1F").floor_id)

    def test_cross_floor_same_room_numbers_both_from_one(self):
        mapping = self.sync([cfg("2F", "书房"), cfg("1F", "书房")])
        self.assertEqual(self.area(mapping, "1F", "书房").name, "书房1")
        self.assertEqual(self.area(mapping, "2F", "书房").name, "书房2")
        self.assertNotEqual(self.area(mapping, "1F", "书房").floor_id,
                            self.area(mapping, "2F", "书房").floor_id)

    def test_reimport_is_idempotent_and_order_independent(self):
        configs = [cfg("1F", "书房"), cfg("2F", "书房")]
        mapping = self.sync(configs)
        events = list(self.areas.events)
        self.assertEqual(self.sync(configs[::-1], mapping), mapping)
        self.assertEqual(self.areas.events, events)

    def test_reuses_matching_existing_floor_and_room(self):
        floor = self.floors.async_create("1F")
        room = self.areas.async_create("厨房", floor_id=floor.floor_id)
        mapping = self.sync([cfg()])
        self.assertEqual(self.area(mapping), room)
        self.assertEqual(len(self.areas.events), 1)

    def test_assigns_unassigned_existing_room(self):
        room = self.areas.async_create("厨房")
        mapping = self.sync([cfg()])
        self.assertEqual(self.area(mapping).id, room.id)
        self.assertIsNotNone(self.area(mapping).floor_id)

    def test_migrates_legacy_combined_area_preserving_id(self):
        room = self.areas.async_create("1F.厨房")
        mapping = self.sync([cfg()])
        self.assertEqual(self.area(mapping).id, room.id)
        self.assertEqual(self.area(mapping).name, "厨房")
        self.assertIsNotNone(self.area(mapping).floor_id)

    def test_does_not_move_same_named_room_from_another_floor(self):
        other = self.floors.async_create("2F")
        room = self.areas.async_create("厨房", floor_id=other.floor_id)
        mapping = self.sync([cfg()])
        self.assertEqual(self.areas.async_get_area(room.id), room)
        self.assertEqual(self.area(mapping).name, "厨房1")
        self.assertNotEqual(self.area(mapping).id, room.id)

    def test_reserves_literal_numbered_rooms(self):
        mapping = self.sync([cfg("1F", "书房"), cfg("2F", "书房"), cfg("3F", "书房1")])
        self.assertEqual({a.name for a in self.areas.items.values()}, {"书房1", "书房2", "书房3"})
        self.assertEqual(self.area(mapping, "3F", "书房1").name, "书房1")

    def test_existing_unrelated_numbered_room_is_not_stolen(self):
        other = self.areas.async_create("厨房1")
        mapping = self.sync([cfg("1F"), cfg("2F")])
        self.assertEqual(self.areas.async_get_area(other.id), other)
        self.assertEqual({self.area(mapping, f).name for f in ("1F", "2F")}, {"厨房2", "厨房3"})

    def test_new_duplicate_renames_old_room_without_replacing_it(self):
        saved = self.sync([cfg()])
        old_id = self.area(saved).id
        updated = self.sync([cfg(), cfg("2F")], saved)
        self.assertEqual(self.area(updated).id, old_id)
        self.assertEqual(self.area(updated).name, "厨房1")
        self.assertEqual(self.area(updated, "2F").name, "厨房2")

    def test_add_literal_suffix_room_releases_occupied_name(self):
        saved = self.sync([cfg("1F"), cfg("2F")])
        old_ids = set(saved.values())
        updated = self.sync([cfg("1F"), cfg("2F"), cfg("3F", "厨房1")], saved)
        self.assertTrue(old_ids <= set(updated.values()))
        self.assertEqual(self.area(updated, "3F", "厨房1").name, "厨房1")
        self.assertEqual(len({a.name for a in self.areas.items.values()}), 3)
        self.assertFalse(any("temporary" in a.name for a in self.areas.items.values()))

    def test_deleted_mapped_area_is_recreated(self):
        saved = self.sync([cfg()])
        old_id = self.area(saved).id
        del self.areas.items[old_id]
        updated = self.sync([cfg()], saved)
        self.assertNotEqual(self.area(updated).id, old_id)
        self.assertEqual(len(self.areas.items), 1)

    def test_periods_do_not_merge_different_location_pairs(self):
        mapping = self.sync([cfg("A.B", "C"), cfg("A", "B.C")])
        self.assertEqual(len(mapping), 2)
        self.assertEqual(len(self.areas.items), 2)

    def test_whitespace_and_case_match_ha_names(self):
        mapping = self.sync([cfg("1 F", "客 厅"), cfg("1f", "客厅")])
        self.assertEqual(len(mapping), 1)
        self.assertEqual(len(self.floors.items), 1)

    def test_partial_reimport_does_not_delete_unreferenced_areas(self):
        saved = self.sync([cfg(), cfg("2F", "卧室")])
        self.sync([cfg()], saved)
        self.assertEqual(len(self.areas.items), 2)


class ValidationTests(unittest.TestCase):
    def test_legacy_yaml_is_unmanaged(self):
        original = {"name": "灯", "suggested_area": "1F.厨房"}
        self.assertIs(locations.validate_location(original), original)

    def test_partial_or_bad_location_fails(self):
        for field in ("floor", "room", "device_id", "name"):
            for value in (None, "", " ", False, 1):
                config = cfg()
                config[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    locations.validate_location(config)
        with self.assertRaises(ValueError):
            locations.validate_location({"name": "灯", "device_id": "1", "room": "厨房"})

    def test_metadata_validation_isolates_bad_entity(self):
        helper = load("entity")
        hass = types.SimpleNamespace(data={"crestron": {"hub": object(), "yaml_config": {
            "light": [cfg(), {**cfg(), "room": ""}],
        }}})
        with self.assertLogs(helper.__name__, level="ERROR"):
            entities = helper.setup_platform_entities(hass, "light", lambda c: c,
                lambda hub, c: types.SimpleNamespace(unique_id="id-1"))
        self.assertEqual(len(entities), 1)
        self.assertEqual(len(hass.data["crestron"][locations.LOCATION_ENTITIES]), 1)


class DeviceSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.floors, self.areas = Floors(), Areas()
        self.devices, self.entities = {}, {}
        self.saved = None
        self.fail_save = False
        self.device_updates, self.entity_updates = [], []
        self.hass = types.SimpleNamespace()
        self.entry = types.SimpleNamespace(entry_id="entry-1")
        owner = self

        class Store:
            def __init__(self, hass, version, key):
                owner.store_key = key
            async def async_load(self):
                return copy.deepcopy(owner.saved)
            async def async_save(self, data):
                if owner.fail_save:
                    raise OSError("disk full")
                owner.saved = copy.deepcopy(data)

        def update_device(identifier, **changes):
            self.device_updates.append((identifier, changes))
            for key, value in changes.items():
                setattr(self.devices[identifier], key, value)

        def update_entity(identifier, **changes):
            self.entity_updates.append((identifier, changes))
            for key, value in changes.items():
                setattr(self.entities[identifier], key, value)

        devices = types.SimpleNamespace(async_get=self.devices.get, async_update_device=update_device)
        entities = types.SimpleNamespace(
            async_get=self.entities.get, async_update_entity=update_entity,
            async_get_entity_id=lambda platform, domain, unique: next(
                (e.entity_id for e in self.entities.values() if e.unique_id == unique), None),
        )
        helpers = types.ModuleType("homeassistant.helpers")
        modules = {"homeassistant.helpers": helpers}
        for name, registry in (("area_registry", self.areas), ("floor_registry", self.floors),
                               ("device_registry", devices), ("entity_registry", entities)):
            mod = types.ModuleType("homeassistant.helpers." + name)
            mod.async_get = lambda hass, r=registry: r
            setattr(helpers, name, mod)
            modules[mod.__name__] = mod
        storage = types.ModuleType("homeassistant.helpers.storage")
        storage.Store = Store
        modules[storage.__name__] = storage
        modules["homeassistant"] = types.ModuleType("homeassistant")
        modules["homeassistant"].helpers = helpers
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def candidate(self, config=None, entity_id="light.old_id", unique="stable-1", device="dev-1"):
        config = config or cfg()
        self.devices[device] = types.SimpleNamespace(
            id=device, identifiers={("crestron", config["device_id"])}, area_id="old-area",
            name_by_user="用户名字",
        )
        self.entities[entity_id] = types.SimpleNamespace(
            entity_id=entity_id, unique_id=unique, device_id=device,
            config_entry_id="entry-1", area_id="entity-override", name="用户实体名字",
        )
        return ("light", unique, config)

    async def sync(self, candidates):
        await locations.async_sync_locations(self.hass, self.entry, candidates)

    async def test_moves_existing_device_preserves_ids_and_custom_names(self):
        candidate = self.candidate()
        await self.sync([candidate])
        self.assertEqual(list(self.entities), ["light.old_id"])
        self.assertEqual(self.entities["light.old_id"].unique_id, "stable-1")
        self.assertEqual(self.entities["light.old_id"].device_id, "dev-1")
        self.assertIsNone(self.entities["light.old_id"].area_id)
        self.assertEqual(self.devices["dev-1"].area_id, next(iter(self.areas.items)))
        self.assertEqual(self.devices["dev-1"].name_by_user, "用户名字")
        self.assertEqual(self.entities["light.old_id"].name, "用户实体名字")
        self.assertEqual(self.store_key, "crestron_locations.entry-1")
        events = list(self.areas.events)
        await self.sync([candidate])
        self.assertEqual(len(self.device_updates), 1)
        self.assertEqual(len(self.entity_updates), 1)
        self.assertEqual(self.areas.events, events)

    async def test_unregistered_and_foreign_entities_do_not_create_rooms(self):
        candidate = self.candidate()
        self.entities["light.old_id"].config_entry_id = "foreign"
        await self.sync([candidate, ("light", "missing", cfg())])
        self.assertEqual(self.floors.items, {})
        self.assertEqual(self.device_updates, [])

    async def test_conflicting_metadata_for_one_device_is_skipped(self):
        first = self.candidate()
        second = self.candidate(cfg(room="书房"), entity_id="light.other", unique="stable-2")
        with self.assertLogs(locations.__name__, level="ERROR"):
            await self.sync([first, second])
        self.assertEqual(self.areas.items, {})
        self.assertEqual(self.device_updates, [])

    async def test_store_failure_is_reported_before_device_move(self):
        self.fail_save = True
        candidate = self.candidate()
        with self.assertRaises(OSError):
            await self.sync([candidate])
        self.assertEqual(self.device_updates, [])
        self.assertEqual(self.entity_updates, [])

    async def test_invalid_saved_map_stops_before_registry_changes(self):
        self.saved = {"areas": []}
        candidate = self.candidate()
        with self.assertRaises(ValueError):
            await self.sync([candidate])
        self.assertEqual(self.areas.items, {})
        self.assertEqual(self.floors.items, {})

    async def test_no_metadata_does_not_require_registries(self):
        await self.sync([])
        self.assertIsNone(self.saved)


if __name__ == "__main__":
    unittest.main()
