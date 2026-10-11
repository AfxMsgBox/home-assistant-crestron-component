"""Synchronize explicit Excel locations through HA's public registries.

Legacy YAML without floor/room is intentionally unmanaged. The saved map binds
an original (floor, room) pair to an area ID, independently of display numbering.
No physical-device commands or entity-ID changes are made here.
"""
from collections import Counter
import json
import logging

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
LOCATION_ENTITIES = "location_entities"


def normalize_name(name):
    """Match HA area/floor registry normalization."""
    return name.casefold().replace(" ", "")


def validate_location(config):
    """Validate explicit location metadata, retaining legacy YAML support."""
    if "floor" not in config and "room" not in config:
        return config
    result = dict(config)
    for field in ("floor", "room", "device_id", "name"):
        value = result.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string for managed locations")
        if field != "device_id":
            result[field] = value.strip()
    return result


def location_key(floor, room):
    return json.dumps([normalize_name(floor), normalize_name(room)], ensure_ascii=False)


def plan_area_names(rooms, selected, reserved):
    """Allocate global area names; preserve previously assigned suffixes.

    rooms: key -> original room name; selected: key -> current area (or None).
    Literal numbered names are reserved before allocating generated suffixes.
    """
    counts = Counter(normalize_name(room) for room in rooms.values())
    occupied = {normalize_name(name) for name in reserved}
    result = {}
    repeated = []
    for key, room in rooms.items():
        if counts[normalize_name(room)] == 1 and normalize_name(room) not in occupied:
            result[key] = room
            occupied.add(normalize_name(room))
        else:
            repeated.append(key)
    # Keep valid existing suffixes to avoid swapping rooms after row reordering.
    for key in repeated:
        area = selected.get(key)
        base = normalize_name(rooms[key])
        current = normalize_name(area.name) if area else ""
        suffix = current[len(base):] if current.startswith(base) else ""
        if suffix.isascii() and suffix.isdigit() and int(suffix) > 0 and current not in occupied:
            result[key] = area.name
            occupied.add(current)
    for key in repeated:
        if key in result:
            continue
        number = 1
        while normalize_name(name := f"{rooms[key]}{number}") in occupied:
            number += 1
        result[key] = name
        occupied.add(normalize_name(name))
    return result


def sync_areas(configs, floors, areas, saved):
    """Reuse/create floor-area pairs and return their ID map (event loop only)."""
    locations = {}
    for cfg in configs:
        locations.setdefault(location_key(cfg["floor"], cfg["room"]),
                             (cfg["floor"], cfg["room"]))
    # Stable ordering for the first import; future imports use the saved map.
    locations = dict(sorted(locations.items()))
    floor_ids = {}
    for floor, _room in locations.values():
        normalized = normalize_name(floor)
        if normalized not in floor_ids:
            existing = floors.async_get_floor_by_name(floor)
            floor_ids[normalized] = (existing or floors.async_create(floor)).floor_id

    selected = {}
    claimed = set()
    # Resolve saved identities first so a new room cannot claim an old area's name.
    for key, (floor, room) in locations.items():
        floor_id = floor_ids[normalize_name(floor)]
        area = areas.async_get_area(saved.get(key)) if saved.get(key) else None
        if area and area.floor_id in (None, floor_id) and area.id not in claimed:
            selected[key] = area
            claimed.add(area.id)
    for key, (floor, room) in locations.items():
        if key in selected:
            continue
        floor_id = floor_ids[normalize_name(floor)]
        plain = areas.async_get_area_by_name(room)
        legacy = areas.async_get_area_by_name(f"{floor}.{room}")
        candidates = [a for a in (plain, legacy) if a and a.floor_id == floor_id]
        candidates += [a for a in (legacy, plain) if a and a.floor_id is None]
        area = next((a for a in candidates if a.id not in claimed), None)
        selected[key] = area
        if area:
            claimed.add(area.id)

    names = plan_area_names(
        {key: room for key, (_floor, room) in locations.items()}, selected,
        [area.name for area in areas.async_list_areas() if area.id not in claimed],
    )
    # Release changing names first, including swaps and literal suffix collisions.
    # Registry changes are synchronous; there is no await inside this batch.
    reserved = {normalize_name(a.name) for a in areas.async_list_areas()}
    reserved.update(normalize_name(name) for name in names.values())
    for key, area in selected.items():
        if area and area.name != names[key]:
            temporary = f"Crestron temporary {area.id}"
            while normalize_name(temporary) in reserved:
                temporary += "_"
            reserved.add(normalize_name(temporary))
            areas.async_update(area.id, name=temporary)

    result = dict(saved)
    for key, (floor, _room) in locations.items():
        floor_id = floor_ids[normalize_name(floor)]
        area = selected[key]
        if area is None:
            area = areas.async_create(names[key], floor_id=floor_id)
        elif area.name != names[key] or area.floor_id != floor_id:
            area = areas.async_update(area.id, name=names[key], floor_id=floor_id)
        result[key] = area.id
    return result


async def async_sync_locations(hass, entry, candidates):
    """Run after entity platforms register devices; touch only accepted entities."""
    if not candidates:
        return
    from homeassistant.helpers import area_registry as ar, device_registry as dr
    from homeassistant.helpers import entity_registry as er, floor_registry as fr
    from homeassistant.helpers.storage import Store

    devices, entities = dr.async_get(hass), er.async_get(hass)
    accepted = []
    per_device = {}
    conflicts = set()
    for platform, unique_id, config in candidates:
        entity_id = entities.async_get_entity_id(platform, DOMAIN, unique_id)
        entity = entities.async_get(entity_id) if entity_id else None
        if not entity or entity.config_entry_id != entry.entry_id or not entity.device_id:
            continue
        device = devices.async_get(entity.device_id)
        if not device or (DOMAIN, config["device_id"]) not in device.identifiers:
            continue
        key = location_key(config["floor"], config["room"])
        if device.id in per_device and per_device[device.id] != key:
            conflicts.add(device.id)
        per_device[device.id] = key
        accepted.append((entity, device, config))
    if conflicts:
        _LOGGER.error("Conflicting floor/room metadata for devices %s; skipping their locations", sorted(conflicts))
    accepted = [item for item in accepted if item[1].id not in conflicts]
    if not accepted:
        return

    store = Store(hass, 1, f"{DOMAIN}_locations.{entry.entry_id}")
    data = await store.async_load()
    saved = data.get("areas", {}) if data is not None else {}
    if not isinstance(saved, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in saved.items()):
        raise ValueError("Invalid saved Crestron location map")
    mapping = sync_areas([cfg for _entity, _device, cfg in accepted],
                         fr.async_get(hass), ar.async_get(hass), saved)
    if mapping != saved:
        await store.async_save({"areas": mapping})
    for entity, device, config in accepted:
        area_id = mapping[location_key(config["floor"], config["room"])]
        if device.area_id != area_id:
            devices.async_update_device(device.id, area_id=area_id)
        # An entity-level override takes precedence over the device's area.
        # Clear it only for entities explicitly managed by this YAML import.
        if entity.area_id is not None:
            entities.async_update_entity(entity.entity_id, area_id=None)
