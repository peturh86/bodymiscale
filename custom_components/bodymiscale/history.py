"""Historical measurement import support for BodyMiScale."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from statistics import fmean
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    async_list_statistic_ids,
)
from homeassistant.const import ATTR_FRIENDLY_NAME, ATTR_UNIT_OF_MEASUREMENT, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BIRTHDAY,
    CONF_IMPEDANCE_MODE,
    CONF_INITIAL_WEIGHT,
    CONF_NEAREST_TOLERANCE,
    CONF_PROFILE_METHOD,
    CONF_WEIGHT_MAX,
    CONF_WEIGHT_MIN,
    DOMAIN,
    HANDLERS,
    IMPEDANCE_MODE_STANDARD,
    PROFILE_METHOD_NEAREST,
    PROFILE_METHOD_NONE,
    PROFILE_METHOD_WEIGHT,
)
from .metrics import BodyScaleMetricsHandler
from .models import Metric

_LOGGER = logging.getLogger(__name__)

_BLUETOOTH_SCALE_DOMAIN = "bluetooth_scale"
_BLUETOOTH_SCALE_HISTORY_SERVICE = "get_measurements"

# Only metrics represented by state_class=measurement sensor entities are
# suitable for Home Assistant long-term statistics.
_STATISTIC_METRICS = frozenset(
    {
        Metric.WEIGHT,
        Metric.IMPEDANCE,
        Metric.BMI,
        Metric.BMR,
        Metric.VISCERAL_FAT,
        Metric.LBM,
        Metric.FAT_PERCENTAGE,
        Metric.WATER_PERCENTAGE,
        Metric.BONE_MASS,
        Metric.MUSCLE_MASS,
        Metric.METABOLIC_AGE,
        Metric.PROTEIN_PERCENTAGE,
        Metric.BODY_SCORE,
        Metric.ECW,
        Metric.ICW,
        Metric.ECW_TBW_RATIO,
        Metric.BCM,
        Metric.SKELETAL_MUSCLE_MASS,
        Metric.IMPEDANCE_LOW,
        Metric.IMPEDANCE_HIGH,
    }
)

_FALLBACK_UNITS: dict[Metric, tuple[str | None, str | None]] = {
    Metric.WEIGHT: ("kg", "mass"),
    Metric.LBM: ("kg", "mass"),
    Metric.BONE_MASS: ("kg", "mass"),
    Metric.MUSCLE_MASS: ("kg", "mass"),
    Metric.BCM: ("kg", "mass"),
    Metric.SKELETAL_MUSCLE_MASS: ("kg", "mass"),
    Metric.FAT_PERCENTAGE: ("%", "unitless"),
    Metric.WATER_PERCENTAGE: ("%", "unitless"),
    Metric.PROTEIN_PERCENTAGE: ("%", "unitless"),
    Metric.ECW_TBW_RATIO: ("%", "unitless"),
    Metric.ECW: ("L", "volume"),
    Metric.ICW: ("L", "volume"),
    Metric.BMR: ("kcal", "energy"),
}


@dataclass(frozen=True, slots=True)
class HistoricalMeasurement:
    """Vendor-neutral historical scale measurement."""

    measurement_id: str
    timestamp: datetime
    weight_kg: float
    impedance_ohm: int | None
    address: str


async def async_import_bluetooth_scale_history(
    hass: HomeAssistant, *, address: str | None = None
) -> dict[str, Any]:
    """Import Bluetooth Scale ledger data into BodyMiScale long-term statistics.

    Exact individual weigh-ins remain owned by Bluetooth Scale. BodyMiScale
    identifies the profile, calculates its metrics, aggregates multiple readings
    in the same UTC hour, and backfills the normal sensor statistic IDs without
    replaying old readings as current Home Assistant states.
    """
    if not hass.services.has_service(
        _BLUETOOTH_SCALE_DOMAIN, _BLUETOOTH_SCALE_HISTORY_SERVICE
    ):
        raise HomeAssistantError(
            "Bluetooth Scale does not expose measurement history. Update and reload "
            "the Bluetooth Scale integration first."
        )

    domain_data = hass.data.get(DOMAIN)
    if not domain_data or not domain_data.get(HANDLERS):
        raise HomeAssistantError("No BodyMiScale profiles are currently loaded")

    service_data = {"address": address} if address else {}
    response = await hass.services.async_call(
        _BLUETOOTH_SCALE_DOMAIN,
        _BLUETOOTH_SCALE_HISTORY_SERVICE,
        service_data,
        blocking=True,
        return_response=True,
    )
    if not isinstance(response, dict):
        raise HomeAssistantError("Bluetooth Scale returned no history response")

    raw_measurements = response.get("measurements")
    if not isinstance(raw_measurements, list):
        raise HomeAssistantError("Bluetooth Scale returned an invalid history response")

    measurements = _parse_measurements(raw_measurements)
    handlers: dict[str, BodyScaleMetricsHandler] = dict(domain_data[HANDLERS])
    if not measurements:
        return {
            "source_measurements": 0,
            "assigned_measurements": 0,
            "unassigned_measurements": 0,
            "profiles": {},
        }

    # Nearest-weight matching is deliberately walked backwards through time.
    # A profile's latest/current weight is a good seed, and each accepted older
    # measurement becomes the next reference point. This follows gradual weight
    # changes without comparing a January measurement only to an August weight.
    tracks: dict[str, float | None] = {}
    for entry_id, handler in handlers.items():
        seed = handler.current_weight
        if seed is None and handler.config.get(CONF_INITIAL_WEIGHT) is not None:
            seed = float(handler.config[CONF_INITIAL_WEIGHT])
        tracks[entry_id] = seed

    assigned: dict[str, list[HistoricalMeasurement]] = defaultdict(list)
    unassigned = 0

    for measurement in sorted(
        measurements, key=lambda item: item.timestamp, reverse=True
    ):
        entry_id = _assign_profile(hass, handlers, tracks, measurement.weight_kg)
        if entry_id is None:
            unassigned += 1
            continue
        assigned[entry_id].append(measurement)
        tracks[entry_id] = measurement.weight_kg

    profile_results: dict[str, Any] = {}
    total_statistic_rows = 0

    for entry_id, profile_measurements in assigned.items():
        handler = handlers[entry_id]
        result = await _async_import_profile_statistics(
            hass, handler, profile_measurements
        )
        profile_results[str(handler.config.get(CONF_NAME, entry_id))] = result
        total_statistic_rows += result["statistic_rows"]

    return {
        "source_measurements": len(measurements),
        "assigned_measurements": sum(len(items) for items in assigned.values()),
        "unassigned_measurements": unassigned,
        "statistic_rows": total_statistic_rows,
        "profiles": profile_results,
    }


def _parse_measurements(items: list[Any]) -> list[HistoricalMeasurement]:
    """Validate the provider response and return usable measurements."""
    parsed: list[HistoricalMeasurement] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = datetime.fromisoformat(str(item["timestamp"]))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
            weight = float(item["weight_kg"])
            impedance_raw = item.get("impedance_ohm")
            impedance = int(impedance_raw) if impedance_raw is not None else None
            measurement_id = str(item["measurement_id"])
            address = str(item.get("address", ""))
        except (KeyError, TypeError, ValueError):
            continue
        parsed.append(
            HistoricalMeasurement(
                measurement_id=measurement_id,
                timestamp=timestamp,
                weight_kg=weight,
                impedance_ohm=impedance,
                address=address,
            )
        )
    return parsed


def _assign_profile(
    hass: HomeAssistant,
    handlers: dict[str, BodyScaleMetricsHandler],
    tracks: dict[str, float | None],
    weight: float,
) -> str | None:
    """Assign a historical measurement to exactly one configured profile."""
    range_matches: list[str] = []
    for entry_id, handler in handlers.items():
        config = handler.config
        if config.get(CONF_PROFILE_METHOD) != PROFILE_METHOD_WEIGHT:
            continue
        w_min = config.get(CONF_WEIGHT_MIN)
        w_max = config.get(CONF_WEIGHT_MAX)
        if w_min is None or w_max is None:
            continue
        if float(w_min) <= weight < float(w_max):
            range_matches.append(entry_id)

    if len(range_matches) == 1:
        return range_matches[0]
    if len(range_matches) > 1:
        _LOGGER.warning(
            "Historical weight %.2f kg matches multiple BodyMiScale weight ranges; "
            "leaving it unassigned",
            weight,
        )
        return None

    nearest: list[tuple[float, str, str]] = []
    for entry_id, handler in handlers.items():
        config = handler.config
        if config.get(CONF_PROFILE_METHOD) != PROFILE_METHOD_NEAREST:
            continue
        reference = tracks.get(entry_id)
        if reference is None:
            continue
        distance = abs(float(reference) - weight)
        tolerance = float(config.get(CONF_NEAREST_TOLERANCE, 5))
        if distance <= tolerance:
            name = str(config.get(CONF_NAME, entry_id)).casefold()
            nearest.append((distance, name, entry_id))

    if nearest:
        nearest.sort()
        return nearest[0][2]

    # A no-filter profile is only deterministic when it is the sole profile.
    if len(handlers) == 1:
        entry_id, handler = next(iter(handlers.items()))
        if handler.config.get(CONF_PROFILE_METHOD) == PROFILE_METHOD_NONE:
            return entry_id

    return None


def _age_at(birthday: str, timestamp: datetime) -> int:
    """Return age on the historical measurement date."""
    try:
        born = datetime.strptime(birthday, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    day = timestamp.date()
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def _calculate_metrics(
    handler: BodyScaleMetricsHandler, measurement: HistoricalMeasurement
) -> dict[Metric, float]:
    """Calculate one historical measurement without mutating live entity state."""
    states: dict[Metric, Any] = {
        Metric.AGE: _age_at(
            str(handler.config.get(CONF_BIRTHDAY, "")), measurement.timestamp
        ),
        Metric.WEIGHT: measurement.weight_kg,
    }

    if (
        measurement.impedance_ohm is not None
        and handler.config.get(CONF_IMPEDANCE_MODE) == IMPEDANCE_MODE_STANDARD
    ):
        states[Metric.IMPEDANCE] = float(measurement.impedance_ohm)

    for metric in handler._topological_order():
        info = handler._dependencies.get(metric)
        if info is None:
            continue

        if metric in (Metric.LBM, Metric.METABOLIC_AGE):
            if Metric.IMPEDANCE not in states:
                continue

        if not all(dep in states for dep in info.depends_on):
            continue

        try:
            value = info.calculate(handler.config, states)
        except (ArithmeticError, TypeError, ValueError):
            _LOGGER.debug(
                "Historical calculation failed for %s at %s",
                metric,
                measurement.timestamp,
                exc_info=True,
            )
            continue
        if value is not None:
            states[metric] = value

    result: dict[Metric, float] = {}
    for metric, value in states.items():
        if metric not in _STATISTIC_METRICS or not isinstance(value, (int, float)):
            continue
        info = handler._dependencies.get(metric)
        precision = info.decimals if info is not None else None
        numeric = float(value)
        result[metric] = round(numeric, precision) if precision is not None else numeric
    return result


async def _async_import_profile_statistics(
    hass: HomeAssistant,
    handler: BodyScaleMetricsHandler,
    measurements: list[HistoricalMeasurement],
) -> dict[str, Any]:
    """Calculate and import one profile's hourly long-term statistics."""
    buckets: dict[Metric, dict[datetime, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for measurement in measurements:
        hour = measurement.timestamp.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        for metric, value in _calculate_metrics(handler, measurement).items():
            buckets[metric][hour].append(value)

    registry = er.async_get(hass)
    metric_entities: dict[Metric, str] = {}
    name = str(handler.config.get(CONF_NAME, handler.config_entry_id))
    for metric in buckets:
        unique_id = "_".join([DOMAIN, name, metric.value])
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is not None:
            metric_entities[metric] = entity_id

    existing_rows = await async_list_statistic_ids(
        hass, set(metric_entities.values()) if metric_entities else set()
    )
    existing_by_id = {
        str(item.get("statistic_id")): item for item in existing_rows
    }

    imported_rows = 0
    imported_metrics = 0

    for metric, entity_id in metric_entities.items():
        hourly = buckets[metric]
        if not hourly:
            continue

        state = hass.states.get(entity_id)
        existing = existing_by_id.get(entity_id, {})
        fallback_unit, fallback_unit_class = _FALLBACK_UNITS.get(
            metric, (None, None)
        )
        unit = existing.get("statistics_unit_of_measurement")
        if unit is None and state is not None:
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit is None:
            unit = fallback_unit

        unit_class = existing.get("unit_class", fallback_unit_class)
        friendly_name = (
            state.attributes.get(ATTR_FRIENDLY_NAME) if state is not None else None
        )

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=existing.get("name") or friendly_name,
            source=str(existing.get("source") or "recorder"),
            statistic_id=entity_id,
            unit_class=unit_class,
            unit_of_measurement=unit,
        )
        statistics = [
            StatisticData(
                start=hour,
                mean=fmean(values),
                min=min(values),
                max=max(values),
            )
            for hour, values in sorted(hourly.items())
        ]
        async_import_statistics(hass, metadata, statistics)
        imported_rows += len(statistics)
        imported_metrics += 1

    return {
        "measurements": len(measurements),
        "metrics": imported_metrics,
        "statistic_rows": imported_rows,
    }
