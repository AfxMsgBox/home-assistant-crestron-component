"""Shared base + setup helper for Crestron platform entities.

Every platform entity repeats the same wiring: never poll, follow the hub's
connection for availability, register a join callback on add / remove it on
remove, and (for the simple ones) just write HA state whenever a subscribed
join changes. This mixin factors that out.

It is a *mixin*, not a base ``Entity``: concrete entities inherit it alongside
the real HA entity class, e.g. ``class CrestronBinarySensor(CrestronEntity,
BinarySensorEntity)``. Listing ``CrestronEntity`` first lets its ``available``
property and ``process_callback`` take precedence. The mixin assumes the
subclass has set ``self._hub`` (done in each subclass ``__init__``, alongside
its own ``unique_id`` / ``device_info`` / join attributes, which stay
per-platform).

Two usage shapes:
  - Simple entities (binary_sensor, sensor, cover, dimmable light, …) only
    override ``_callback_joins()`` to declare what they listen to; the default
    ``async_added_to_hass`` registers them and ``process_callback`` writes
    state.
  - Entities that restore/reconcile initial state (switch, select, number,
    climate, on/off light) override ``async_added_to_hass`` — calling
    ``await super().async_added_to_hass()`` to register, then reading initial
    state — and usually override ``process_callback`` to reconcile with
    feedback. The join list still lives in ``_callback_joins()``.
"""

import logging

from .const import HUB, DOMAIN, YAML_CONF

_LOGGER = logging.getLogger(__name__)


def setup_platform_entities(hass, platform_key, schema, factory):
    """Build entities for one platform from YAML, isolating per-entity errors.

    Replaces the repeated ``async_add_entities(Entity(hub, SCHEMA(item)) for
    item in items)`` one-liner in each platform. Validating inside a generator
    means one malformed entry aborts the whole platform; here each entry is
    validated and built in its own try/except, so a single bad entity is
    skipped (with a uniform warning) and the rest still load.

    ``schema`` validates one raw config dict; ``factory(hub, cfg)`` builds the
    entity from the validated config (a plain class works directly; ``light``
    passes a factory that picks dimmable vs on/off). Returns the entity list.
    """
    hub = hass.data[DOMAIN][HUB]
    items = hass.data[DOMAIN][YAML_CONF].get(platform_key, [])
    entities = []
    for index, item in enumerate(items):
        try:
            entities.append(factory(hub, schema(item)))
        except Exception:
            name = item.get("name") if isinstance(item, dict) else None
            _LOGGER.exception(
                "Skipping invalid %s entity #%d (%s)",
                platform_key,
                index,
                name or "unnamed",
            )
    return entities


class CrestronEntity:
    _attr_should_poll = False

    @property
    def available(self):
        return self._hub.is_available()

    def _callback_joins(self):
        """Join keys this entity subscribes to (e.g. ``["d5", "a3"]``).

        Override in each platform. The default registration in
        ``async_added_to_hass`` uses this list.
        """
        return []

    async def async_added_to_hass(self):
        self._hub.register_callback(
            self.process_callback, joins=self._callback_joins()
        )

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)

    async def process_callback(self, cbtype, value):
        """Default: a subscribed join changed, re-render from the cache.

        Entities with optimistic/feedback reconciliation override this.
        """
        self.async_write_ha_state()
