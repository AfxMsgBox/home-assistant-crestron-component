"""Bridges between Home Assistant and the XSIG protocol layer.

``CrestronHub`` used to do everything in one ``__init__``. The two directions
of data flow are independent and are split out here:

  - ``ToJoinBridge`` — HA entity state / templates -> XSIG joins. Wraps each
    ``to_joins`` entry in a Template, tracks results, and pushes coerced values
    to the control system. Also re-renders every join on demand (the control
    system's sync-all request).
  - ``FromJoinBridge`` — XSIG joins -> HA scripts. Registers a join callback
    and runs the configured ``from_joins`` script when a join changes.

``CrestronHub`` (in ``__init__.py``) now just owns the ``CrestronXsig`` instance
and the server lifecycle, and composes these two bridges.
"""

import logging

from homeassistant.helpers.event import TrackTemplate, async_track_template_result
from homeassistant.helpers.template import Template
from homeassistant.helpers.script import Script
from homeassistant.core import callback, Context
from homeassistant.const import CONF_VALUE_TEMPLATE, CONF_ATTRIBUTE, CONF_ENTITY_ID

from .const import CONF_JOIN, CONF_SCRIPT, CONF_TO_HUB, CONF_FROM_HUB, DOMAIN
from .value_coercion import resolve_join_write

_LOGGER = logging.getLogger(__name__)


def _build_template(hass, entity):
    """Build the Template for one to_joins entry, or None if none applies.

    Precedence: an explicit value_template wins; otherwise state_attr when an
    attribute is named; otherwise the entity's plain state.
    """
    if CONF_VALUE_TEMPLATE in entity:
        return entity[CONF_VALUE_TEMPLATE]
    if CONF_ATTRIBUTE in entity and CONF_ENTITY_ID in entity:
        return Template(
            "{{state_attr('"
            + entity[CONF_ENTITY_ID]
            + "','"
            + entity[CONF_ATTRIBUTE]
            + "')}}",
            hass,
        )
    if CONF_ENTITY_ID in entity:
        return Template(
            "{{states('" + entity[CONF_ENTITY_ID] + "')}}", hass
        )
    return None


class ToJoinBridge:
    """HA entity state / templates -> XSIG joins."""

    def __init__(self, hass, hub, entries):
        self.hass = hass
        self.hub = hub
        self._join_to_template = {}  # join key -> Template
        self._template_to_join = {}  # id(template) -> join key
        self._tracker = None
        for entity in entries or []:
            template = _build_template(hass, entity)
            if template is not None:
                join = entity[CONF_JOIN]
                self._join_to_template[join] = template
                self._template_to_join[id(template)] = join

    def start(self):
        """Begin tracking template results (no-op if nothing to track)."""
        if not self._join_to_template:
            return
        track_templates = [
            TrackTemplate(t, None) for t in self._join_to_template.values()
        ]
        self._tracker = async_track_template_result(
            self.hass, track_templates, self._template_change
        )

    def stop(self):
        if self._tracker is not None:
            self._tracker.async_remove()
            self._tracker = None

    def _set_join(self, key, result):
        """Coerce a template result and send it to the control system."""
        try:
            resolved = resolve_join_write(key, result)
        except ValueError:
            _LOGGER.warning(f"Invalid join key: {key}")
            return
        if resolved is None:
            return
        kind, number, value = resolved
        if kind == "d":
            self.hub.set_digital(number, value)
        elif kind == "a":
            self.hub.set_analog(number, value)
        elif kind == "s":
            self.hub.set_serial(number, value)

    @callback
    def _template_change(self, event, updates):
        for track_template_result in updates:
            join = self._template_to_join.get(id(track_template_result.template))
            if join is not None:
                self._set_join(join, track_template_result.result)

    def sync_all(self):
        """Re-render and resend every join (control system's sync-all request)."""
        _LOGGER.debug("Syncing joins to control system")
        for join, template in self._join_to_template.items():
            # Isolate per-join failures: a single bad template (e.g. referencing
            # an unknown entity attribute) must not abort the rest of the sync
            # or bubble up and tear down the XSIG connection.
            try:
                self._set_join(join, template.async_render())
            except Exception:
                _LOGGER.exception("Failed to sync join %s to control system", join)


class FromJoinBridge:
    """XSIG joins -> HA scripts."""

    def __init__(self, hass, hub, entries):
        self.hass = hass
        self.hub = hub
        self.context = Context()
        self._scripts = {}  # join key -> Script
        for entry in entries or []:
            self._scripts[entry[CONF_JOIN]] = Script(
                hass, entry[CONF_SCRIPT], f"Crestron {entry[CONF_JOIN]}", DOMAIN
            )

    def start(self):
        if self._scripts:
            self.hub.register_callback(
                self._join_change, joins=list(self._scripts.keys())
            )

    def stop(self):
        if self._scripts:
            self.hub.remove_callback(self._join_change)

    async def _join_change(self, cbtype, value):
        script = self._scripts.get(cbtype)
        if script is None:
            return
        # For digital joins, only fire on rising edge (1) to avoid
        # double-trigger from momentary buttons.
        if cbtype[:1] == "d" and value == "0":
            return
        _LOGGER.debug(f"Running script for {cbtype} = {value}")
        # Run in background so a slow script can't block XSIG dispatch / TCP read.
        self.hass.async_create_task(self._run_script(script, cbtype, value))

    async def _run_script(self, script, cbtype, value):
        try:
            await script.async_run({"value": value}, self.context)
        except Exception:
            _LOGGER.exception("from_joins script for %s failed", cbtype)
