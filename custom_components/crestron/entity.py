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


def join_uid(analog=(), digital=()):
    """Build the join-derived part of a ``unique_id``, namespace-safe.

    Analog and digital joins are *independent* numbering spaces in XSIG
    (a1..a1024 and d1..d4096 are unrelated signals), so a platform that picks
    "the first join that happens to be configured" out of a mixed list can hand
    two different entities the same id — at which point Home Assistant drops
    whichever one is registered second, with only a "does not generate unique
    IDs" warning to show for it.

    ``analog`` and ``digital`` are the candidate joins in preference order.
    Analog candidates keep the bare number, digital ones get a ``d`` prefix;
    that is enough to separate the two spaces, and it keeps ids stable for
    entities that were already resolving to an analog join (the common case),
    so no entity migration is needed.

    Returns ``None`` if no candidate is configured — callers should not let
    that happen (their schemas require at least one join).
    """
    for join in analog:
        if join is not None:
            return str(join)
    for join in digital:
        if join is not None:
            return f"d{join}"
    return None


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
    if not isinstance(items, list):
        _LOGGER.error(
            "`%s:` under `crestron:` must be a list of entities, got %s — no "
            "%s entities were created",
            platform_key,
            type(items).__name__,
            platform_key,
        )
        return []
    entities = []
    claimed = {}  # unique_id -> name of the entity that got it
    for index, item in enumerate(items):
        try:
            entity = factory(hub, schema(item))
        except Exception:
            name = item.get("name") if isinstance(item, dict) else None
            _LOGGER.exception(
                "Skipping invalid %s entity #%d (%s)",
                platform_key,
                index,
                name or "unnamed",
            )
            continue
        # Home Assistant drops a colliding entity itself, but only says "does
        # not generate unique IDs" — naming both configs is the difference
        # between a two-minute fix and a hunt. Skipping here also keeps the
        # winner deterministic (first in YAML) instead of registration-order.
        unique_id = getattr(entity, "unique_id", None)
        if unique_id is not None and unique_id in claimed:
            _LOGGER.error(
                "Skipping %s entity #%d (%s): unique ID %r is already used by "
                "%r. Two entities deriving an ID from the same join collide; "
                "give one of them a different control join.",
                platform_key,
                index,
                (item.get("name") if isinstance(item, dict) else None)
                or "unnamed",
                unique_id,
                claimed[unique_id],
            )
            continue
        if unique_id is not None:
            claimed[unique_id] = (
                item.get("name") if isinstance(item, dict) else None
            ) or f"#{index}"
        entities.append(entity)
    return entities


class CrestronEntity:
    _attr_should_poll = False
    # Set while a coalesced state write is pending on the event loop.
    _write_scheduled = False
    _removed = False

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
        # Home Assistant builds fresh entities on reload, but clear the flag
        # anyway so a re-added instance isn't left permanently unable to write.
        self._removed = False
        self._hub.register_callback(
            self.process_callback, joins=self._callback_joins()
        )

    async def async_will_remove_from_hass(self):
        self._hub.remove_callback(self.process_callback)
        # Drop any pending flush: writing state after removal raises.
        self._removed = True
        self._write_scheduled = False

    def _schedule_write(self):
        """Write HA state once per event-loop iteration, not once per join.

        The control system pushes each join as its own frame, so an entity
        watching several joins is called back several times for what is really
        one update — a climate subscribing to 12 joins writes state 12 times
        during the cold-start full sync, and every write builds a State object,
        fires an event and hits the recorder. Reconciling is cheap; writing is
        not. Callbacks therefore do their decision work eagerly (so the cached
        values are always current) and mark the entity dirty; the actual write
        happens once, after the current burst of frames has been processed.

        Only *feedback-driven* writes are coalesced. Command paths
        (``async_turn_on`` and friends) keep calling ``async_write_ha_state``
        directly, because there the whole point is to show the optimistic state
        with no delay.
        """
        if self._write_scheduled:
            return
        hass = getattr(self, "hass", None)
        if hass is None:
            # Not attached to Home Assistant (nothing to coalesce against).
            self.async_write_ha_state()
            return
        self._write_scheduled = True
        hass.loop.call_soon(self._flush_write)

    def _flush_write(self):
        if not self._write_scheduled or self._removed:
            return
        self._write_scheduled = False
        self.async_write_ha_state()

    async def process_callback(self, cbtype, value):
        """Default: a subscribed join changed, re-render from the cache.

        Entities with optimistic/feedback reconciliation override this.
        """
        self._schedule_write()
