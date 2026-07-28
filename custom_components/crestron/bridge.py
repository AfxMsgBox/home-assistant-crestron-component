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
from .crestron import AVAILABLE_KEY
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
        # One socket write for the whole sync instead of one per join.
        with self.hub.batched_writes():
            for join, template in self._join_to_template.items():
                # Isolate per-join failures: a single bad template (e.g.
                # referencing an unknown entity attribute) must not abort the
                # rest of the sync or bubble up and tear down the connection.
                try:
                    self._set_join(join, template.async_render())
                except Exception:
                    _LOGGER.exception(
                        "Failed to sync join %s to control system", join
                    )


class FromJoinBridge:
    """XSIG joins -> HA scripts."""

    def __init__(self, hass, hub, entries):
        self.hass = hass
        self.hub = hub
        self.context = Context()
        self._scripts = {}  # join key -> Script
        # Last value seen per digital join, for real edge detection. Empty at
        # start, which is what makes the initial full sync inert (see below).
        self._last_digital = {}
        for entry in entries or []:
            join = entry[CONF_JOIN]
            if join in self._scripts:
                _LOGGER.warning(
                    "Duplicate from_joins entry for %s — only the last script "
                    "will run",
                    join,
                )
            self._scripts[join] = Script(
                hass, entry[CONF_SCRIPT], f"Crestron {join}", DOMAIN
            )

    def start(self):
        if self._scripts:
            self.hub.register_callback(
                self._join_change, joins=list(self._scripts.keys())
            )

    def stop(self):
        if self._scripts:
            self.hub.remove_callback(self._join_change)
        # Drop edge history: after a reload the joins have to be observed again
        # before anything counts as a transition.
        self._last_digital.clear()

    def _is_rising_edge(self, cbtype, value):
        """True only for a genuine 0 -> 1 transition on a digital join.

        Testing ``value != "0"`` is not edge detection — it fires on *any*
        report of a high join. The control system answers our 0xFD with the
        current level of every join, so on every connect (HA restart, control
        system reboot, a dropped TCP session) every button-style join that
        happens to be latched high would run its script: scenes replayed, lights
        commanded on, at the worst possible moment.

        A join whose previous value we have never seen is therefore *not* an
        edge. That deliberately makes the first report after a connect inert,
        at the cost of missing a press that happens in the same instant the
        connection comes up — the safe direction to be wrong in.
        """
        previous = self._last_digital.get(cbtype)
        self._last_digital[cbtype] = value
        return previous == "0" and value == "1"

    async def _join_change(self, cbtype, value):
        # Availability flips bracket every connection, including a plain TCP
        # reconnect that never tears this bridge down. The baseline has to go
        # with them: a join recorded low before the drop and re-reported high
        # by the reconnect's full sync would otherwise look like a real press,
        # which is the exact false trigger the edge detection exists to stop.
        if cbtype == AVAILABLE_KEY:
            self._last_digital.clear()
            return
        script = self._scripts.get(cbtype)
        if script is None:
            return
        if cbtype[:1] == "d" and not self._is_rising_edge(cbtype, value):
            return
        _LOGGER.debug(f"Running script for {cbtype} = {value}")
        # Run in background so a slow script can't block XSIG dispatch / TCP read.
        self.hass.async_create_task(self._run_script(script, cbtype, value))

    async def _run_script(self, script, cbtype, value):
        try:
            await script.async_run({"value": value}, self.context)
        except Exception:
            _LOGGER.exception("from_joins script for %s failed", cbtype)
