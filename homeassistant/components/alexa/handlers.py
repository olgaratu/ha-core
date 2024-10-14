"""Alexa message handlers."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
import logging
import math
from typing import Any

from homeassistant import core as ha
from homeassistant.components import (
    button,
    camera,
    climate,
    cover,
    fan,
    group,
    humidifier,
    input_button,
    input_number,
    light,
    media_player,
    number,
    remote,
    timer,
    vacuum,
    valve,
    water_heater,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ENTITY_PICTURE,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    SERVICE_ALARM_ARM_AWAY,
    SERVICE_ALARM_ARM_HOME,
    SERVICE_ALARM_ARM_NIGHT,
    SERVICE_ALARM_DISARM,
    SERVICE_LOCK,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_STOP,
    SERVICE_SET_COVER_POSITION,
    SERVICE_SET_COVER_TILT_POSITION,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_UNLOCK,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_ALARM_DISARMED,
    UnitOfTemperature,
)
from homeassistant.helpers import network
from homeassistant.util import color as color_util, dt as dt_util
from homeassistant.util.decorator import Registry
from homeassistant.util.unit_conversion import TemperatureConverter

from .config import AbstractConfig
from .const import (
    API_TEMP_UNITS,
    API_THERMOSTAT_MODES,
    API_THERMOSTAT_MODES_CUSTOM,
    API_THERMOSTAT_PRESETS,
    DATE_FORMAT,
    Cause,
    Inputs,
)
from .entities import async_get_entities
from .errors import (
    AlexaInvalidDirectiveError,
    AlexaInvalidValueError,
    AlexaSecurityPanelAuthorizationRequired,
    AlexaTempRangeError,
    AlexaUnsupportedThermostatModeError,
    AlexaUnsupportedThermostatTargetStateError,
    AlexaVideoActionNotPermittedForContentError,
)
from .state_report import AlexaDirective, AlexaResponse, async_enable_proactive_mode

_LOGGER = logging.getLogger(__name__)
DIRECTIVE_NOT_SUPPORTED = "Entity does not support directive"

MIN_MAX_TEMP = {
    climate.DOMAIN: {
        "min_temp": climate.ATTR_MIN_TEMP,
        "max_temp": climate.ATTR_MAX_TEMP,
    },
    water_heater.DOMAIN: {
        "min_temp": water_heater.ATTR_MIN_TEMP,
        "max_temp": water_heater.ATTR_MAX_TEMP,
    },
}

SERVICE_SET_TEMPERATURE = {
    climate.DOMAIN: climate.SERVICE_SET_TEMPERATURE,
    water_heater.DOMAIN: water_heater.SERVICE_SET_TEMPERATURE,
}

HANDLERS: Registry[
    tuple[str, str],
    Callable[
        [ha.HomeAssistant, AbstractConfig, AlexaDirective, ha.Context],
        Coroutine[Any, Any, AlexaResponse],
    ],
] = Registry()


@HANDLERS.register(("Alexa.Discovery", "Discover"))
async def async_api_discovery(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Create a API formatted discovery response.

    Async friendly.
    """
    discovery_endpoints: list[dict[str, Any]] = []
    for alexa_entity in async_get_entities(hass, config):
        if not config.should_expose(alexa_entity.entity_id):
            continue
        try:
            discovered_serialized_entity = alexa_entity.serialize_discovery()
        except Exception:
            _LOGGER.exception(
                "Unable to serialize %s for discovery", alexa_entity.entity_id
            )
        else:
            discovery_endpoints.append(discovered_serialized_entity)

    return directive.response(
        name="Discover.Response",
        namespace="Alexa.Discovery",
        payload={"endpoints": discovery_endpoints},
    )


@HANDLERS.register(("Alexa.Authorization", "AcceptGrant"))
async def async_api_accept_grant(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Create a API formatted AcceptGrant response.

    Async friendly.
    """
    auth_code: str = directive.payload["grant"]["code"]

    if config.supports_auth:
        await config.async_accept_grant(auth_code)

        if config.should_report_state:
            await async_enable_proactive_mode(hass, config)

    return directive.response(
        name="AcceptGrant.Response", namespace="Alexa.Authorization", payload={}
    )


@HANDLERS.register(("Alexa.PowerController", "TurnOn"))
async def async_api_turn_on(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a turn on request."""
    entity = directive.entity
    domain = _get_domain(entity)

    service = await _get_service_for_turn_on(hass, entity, domain)

    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity.entity_id},
        blocking=False,
        context=context,
    )

    return directive.response()


def _get_domain(entity: ha.State) -> str:
    """Return the appropriate domain for the entity."""
    if entity.domain == group.DOMAIN:
        return ha.DOMAIN
    return entity.domain


async def _get_service_for_turn_on(
    hass: ha.HomeAssistant, entity: ha.State, domain: str
) -> str:
    """Return the appropriate service to call for turning on an entity."""
    if domain == cover.DOMAIN:
        return cover.SERVICE_OPEN_COVER
    if domain == climate.DOMAIN:
        return climate.SERVICE_TURN_ON
    if domain == fan.DOMAIN:
        return fan.SERVICE_TURN_ON
    if domain == humidifier.DOMAIN:
        return humidifier.SERVICE_TURN_ON
    if domain == remote.DOMAIN:
        return remote.SERVICE_TURN_ON
    if domain == vacuum.DOMAIN:
        return await _get_vacuum_service(entity)
    if domain == timer.DOMAIN:
        return timer.SERVICE_START
    if domain == media_player.DOMAIN:
        return await _get_media_player_service(entity)
    return SERVICE_TURN_ON


async def _get_vacuum_service(entity: ha.State) -> str:
    """Determine the appropriate vacuum service to call based on supported features."""
    supported = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
    if (
        not supported & vacuum.VacuumEntityFeature.TURN_ON
        and supported & vacuum.VacuumEntityFeature.START
    ):
        return vacuum.SERVICE_START
    return vacuum.SERVICE_TURN_ON


async def _get_media_player_service(entity: ha.State) -> str:
    """Determine the appropriate media player service to call."""
    supported = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
    power_features = (
        media_player.MediaPlayerEntityFeature.TURN_ON
        | media_player.MediaPlayerEntityFeature.TURN_OFF
    )
    if not supported & power_features:
        return media_player.SERVICE_MEDIA_PLAY
    return SERVICE_TURN_ON


@HANDLERS.register(("Alexa.PowerController", "TurnOff"))
async def async_api_turn_off(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a turn off request."""
    entity = directive.entity
    domain = entity.domain
    if entity.domain == group.DOMAIN:
        domain = ha.DOMAIN

    service = SERVICE_TURN_OFF
    if entity.domain == cover.DOMAIN:
        service = cover.SERVICE_CLOSE_COVER
    elif domain == climate.DOMAIN:
        service = climate.SERVICE_TURN_OFF
    elif domain == fan.DOMAIN:
        service = fan.SERVICE_TURN_OFF
    elif domain == remote.DOMAIN:
        service = remote.SERVICE_TURN_OFF
    elif domain == humidifier.DOMAIN:
        service = humidifier.SERVICE_TURN_OFF
    elif domain == vacuum.DOMAIN:
        supported = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        if (
            not supported & vacuum.VacuumEntityFeature.TURN_OFF
            and supported & vacuum.VacuumEntityFeature.RETURN_HOME
        ):
            service = vacuum.SERVICE_RETURN_TO_BASE
    elif domain == timer.DOMAIN:
        service = timer.SERVICE_CANCEL
    elif domain == media_player.DOMAIN:
        supported = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        power_features = (
            media_player.MediaPlayerEntityFeature.TURN_ON
            | media_player.MediaPlayerEntityFeature.TURN_OFF
        )
        if not supported & power_features:
            service = media_player.SERVICE_MEDIA_STOP

    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity.entity_id},
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.BrightnessController", "SetBrightness"))
async def async_api_set_brightness(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set brightness request."""
    entity = directive.entity
    brightness = int(directive.payload["brightness"])

    await hass.services.async_call(
        entity.domain,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity.entity_id, light.ATTR_BRIGHTNESS_PCT: brightness},
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.BrightnessController", "AdjustBrightness"))
async def async_api_adjust_brightness(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an adjust brightness request."""
    entity = directive.entity
    brightness_delta = int(directive.payload["brightnessDelta"])

    # set brightness
    await hass.services.async_call(
        entity.domain,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: entity.entity_id,
            light.ATTR_BRIGHTNESS_STEP_PCT: brightness_delta,
        },
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.ColorController", "SetColor"))
async def async_api_set_color(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set color request."""
    entity = directive.entity
    rgb = color_util.color_hsb_to_RGB(
        float(directive.payload["color"]["hue"]),
        float(directive.payload["color"]["saturation"]),
        float(directive.payload["color"]["brightness"]),
    )

    await hass.services.async_call(
        entity.domain,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity.entity_id, light.ATTR_RGB_COLOR: rgb},
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.ColorTemperatureController", "SetColorTemperature"))
async def async_api_set_color_temperature(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set color temperature request."""
    entity = directive.entity
    kelvin = int(directive.payload["colorTemperatureInKelvin"])

    await hass.services.async_call(
        entity.domain,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity.entity_id, light.ATTR_KELVIN: kelvin},
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.ColorTemperatureController", "DecreaseColorTemperature"))
async def async_api_decrease_color_temp(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a decrease color temperature request."""
    entity = directive.entity
    current = int(entity.attributes[light.ATTR_COLOR_TEMP])
    max_mireds = int(entity.attributes[light.ATTR_MAX_MIREDS])

    value = min(max_mireds, current + 50)
    await hass.services.async_call(
        entity.domain,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity.entity_id, light.ATTR_COLOR_TEMP: value},
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.ColorTemperatureController", "IncreaseColorTemperature"))
async def async_api_increase_color_temp(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an increase color temperature request."""
    entity = directive.entity
    current = int(entity.attributes[light.ATTR_COLOR_TEMP])
    min_mireds = int(entity.attributes[light.ATTR_MIN_MIREDS])

    value = max(min_mireds, current - 50)
    await hass.services.async_call(
        entity.domain,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity.entity_id, light.ATTR_COLOR_TEMP: value},
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.SceneController", "Activate"))
async def async_api_activate(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an activate request."""
    entity = directive.entity
    domain = entity.domain

    service = SERVICE_TURN_ON
    if domain == button.DOMAIN:
        service = button.SERVICE_PRESS
    elif domain == input_button.DOMAIN:
        service = input_button.SERVICE_PRESS

    await hass.services.async_call(
        domain,
        service,
        {ATTR_ENTITY_ID: entity.entity_id},
        blocking=False,
        context=context,
    )

    payload: dict[str, Any] = {
        "cause": {"type": Cause.VOICE_INTERACTION},
        "timestamp": dt_util.utcnow().strftime(DATE_FORMAT),
    }

    return directive.response(
        name="ActivationStarted", namespace="Alexa.SceneController", payload=payload
    )


@HANDLERS.register(("Alexa.SceneController", "Deactivate"))
async def async_api_deactivate(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a deactivate request."""
    entity = directive.entity
    domain = entity.domain

    await hass.services.async_call(
        domain,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity.entity_id},
        blocking=False,
        context=context,
    )

    payload: dict[str, Any] = {
        "cause": {"type": Cause.VOICE_INTERACTION},
        "timestamp": dt_util.utcnow().strftime(DATE_FORMAT),
    }

    return directive.response(
        name="DeactivationStarted", namespace="Alexa.SceneController", payload=payload
    )


@HANDLERS.register(("Alexa.LockController", "Lock"))
async def async_api_lock(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a lock request."""
    entity = directive.entity
    await hass.services.async_call(
        entity.domain,
        SERVICE_LOCK,
        {ATTR_ENTITY_ID: entity.entity_id},
        blocking=False,
        context=context,
    )

    response = directive.response()
    response.add_context_property(
        {"name": "lockState", "namespace": "Alexa.LockController", "value": "LOCKED"}
    )
    return response


@HANDLERS.register(("Alexa.LockController", "Unlock"))
async def async_api_unlock(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an unlock request."""
    if config.locale not in {
        "ar-SA",
        "de-DE",
        "en-AU",
        "en-CA",
        "en-GB",
        "en-IN",
        "en-US",
        "es-ES",
        "es-MX",
        "es-US",
        "fr-CA",
        "fr-FR",
        "hi-IN",
        "it-IT",
        "ja-JP",
        "pt-BR",
    }:
        msg = (
            "The unlock directive is not supported for the following locales:"
            f" {config.locale}"
        )
        raise AlexaInvalidDirectiveError(msg)

    entity = directive.entity
    await hass.services.async_call(
        entity.domain,
        SERVICE_UNLOCK,
        {ATTR_ENTITY_ID: entity.entity_id},
        blocking=False,
        context=context,
    )

    response = directive.response()
    response.add_context_property(
        {"namespace": "Alexa.LockController", "name": "lockState", "value": "UNLOCKED"}
    )

    return response


@HANDLERS.register(("Alexa.Speaker", "SetVolume"))
async def async_api_set_volume(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set volume request."""
    volume = round(float(directive.payload["volume"] / 100), 2)
    entity = directive.entity

    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        media_player.const.ATTR_MEDIA_VOLUME_LEVEL: volume,
    }

    await hass.services.async_call(
        entity.domain, SERVICE_VOLUME_SET, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.InputController", "SelectInput"))
async def async_api_select_input(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set input request."""
    media_input = directive.payload["input"]
    entity = directive.entity

    # Attempt to map the ALL UPPERCASE payload name to a source.
    # Strips trailing 1 to match single input devices.
    source_list = entity.attributes.get(media_player.const.ATTR_INPUT_SOURCE_LIST) or []
    for source in source_list:
        formatted_source = (
            source.lower().replace("-", "").replace("_", "").replace(" ", "")
        )
        media_input = media_input.lower().replace(" ", "")
        if (
            formatted_source in Inputs.VALID_SOURCE_NAME_MAP
            and formatted_source == media_input
        ) or (
            media_input.endswith("1") and formatted_source == media_input.rstrip("1")
        ):
            media_input = source
            break
    else:
        msg = (
            f"failed to map input {media_input} to a media source on {entity.entity_id}"
        )
        raise AlexaInvalidValueError(msg)

    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        media_player.const.ATTR_INPUT_SOURCE: media_input,
    }

    await hass.services.async_call(
        entity.domain,
        media_player.SERVICE_SELECT_SOURCE,
        data,
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.Speaker", "AdjustVolume"))
async def async_api_adjust_volume(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an adjust volume request."""
    volume_delta = int(directive.payload["volume"])

    entity = directive.entity
    current_level = entity.attributes[media_player.const.ATTR_MEDIA_VOLUME_LEVEL]

    # read current state
    try:
        current = math.floor(int(current_level * 100))
    except ZeroDivisionError:
        current = 0

    volume = float(max(0, volume_delta + current) / 100)

    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        media_player.const.ATTR_MEDIA_VOLUME_LEVEL: volume,
    }

    await hass.services.async_call(
        entity.domain, SERVICE_VOLUME_SET, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.StepSpeaker", "AdjustVolume"))
async def async_api_adjust_volume_step(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an adjust volume step request."""
    # media_player volume up/down service does not support specifying steps
    # each component handles it differently e.g. via config.
    # This workaround will simply call the volume up/Volume down the amount of
    # steps asked for. When no steps are called in the request, Alexa sends
    # a default of 10 steps which for most purposes is too high. The default
    # is set 1 in this case.
    entity = directive.entity
    volume_int = int(directive.payload["volumeSteps"])
    is_default = bool(directive.payload["volumeStepsDefault"])
    default_steps = 1

    if volume_int < 0:
        service_volume = SERVICE_VOLUME_DOWN
        if is_default:
            volume_int = -default_steps
    else:
        service_volume = SERVICE_VOLUME_UP
        if is_default:
            volume_int = default_steps

    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    for _ in range(abs(volume_int)):
        await hass.services.async_call(
            entity.domain, service_volume, data, blocking=False, context=context
        )

    return directive.response()


@HANDLERS.register(("Alexa.StepSpeaker", "SetMute"))
@HANDLERS.register(("Alexa.Speaker", "SetMute"))
async def async_api_set_mute(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set mute request."""
    mute = bool(directive.payload["mute"])
    entity = directive.entity
    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        media_player.const.ATTR_MEDIA_VOLUME_MUTED: mute,
    }

    await hass.services.async_call(
        entity.domain, SERVICE_VOLUME_MUTE, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.PlaybackController", "Play"))
async def async_api_play(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a play request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    await hass.services.async_call(
        entity.domain, SERVICE_MEDIA_PLAY, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.PlaybackController", "Pause"))
async def async_api_pause(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a pause request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    await hass.services.async_call(
        entity.domain, SERVICE_MEDIA_PAUSE, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.PlaybackController", "Stop"))
async def async_api_stop(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a stop request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    await hass.services.async_call(
        entity.domain, SERVICE_MEDIA_STOP, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.PlaybackController", "Next"))
async def async_api_next(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a next request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    await hass.services.async_call(
        entity.domain, SERVICE_MEDIA_NEXT_TRACK, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.PlaybackController", "Previous"))
async def async_api_previous(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a previous request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    await hass.services.async_call(
        entity.domain,
        SERVICE_MEDIA_PREVIOUS_TRACK,
        data,
        blocking=False,
        context=context,
    )

    return directive.response()


def temperature_from_object(
    hass: ha.HomeAssistant, temp_obj: dict[str, Any], interval: bool = False
) -> float:
    """Get temperature from Temperature object in requested unit."""
    to_unit = hass.config.units.temperature_unit
    from_unit = UnitOfTemperature.CELSIUS
    temp = float(temp_obj["value"])

    if temp_obj["scale"] == "FAHRENHEIT":
        from_unit = UnitOfTemperature.FAHRENHEIT
    elif temp_obj["scale"] == "KELVIN" and not interval:
        # convert to Celsius if absolute temperature
        temp -= 273.15

    if interval:
        return TemperatureConverter.convert_interval(temp, from_unit, to_unit)
    return TemperatureConverter.convert(temp, from_unit, to_unit)


@HANDLERS.register(("Alexa.ThermostatController", "SetTargetTemperature"))
async def async_api_set_target_temp(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set target temperature request."""
    entity = directive.entity
    domain = entity.domain

    min_temp = entity.attributes[MIN_MAX_TEMP[domain]["min_temp"]]
    max_temp = entity.attributes["max_temp"]
    unit = hass.config.units.temperature_unit

    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    payload = directive.payload
    response = directive.response()
    if "targetSetpoint" in payload:
        temp = temperature_from_object(hass, payload["targetSetpoint"])
        if temp < min_temp or temp > max_temp:
            raise AlexaTempRangeError(hass, temp, min_temp, max_temp)
        data[ATTR_TEMPERATURE] = temp
        response.add_context_property(
            {
                "name": "targetSetpoint",
                "namespace": "Alexa.ThermostatController",
                "value": {"value": temp, "scale": API_TEMP_UNITS[unit]},
            }
        )
    if "lowerSetpoint" in payload:
        temp_low = temperature_from_object(hass, payload["lowerSetpoint"])
        if temp_low < min_temp or temp_low > max_temp:
            raise AlexaTempRangeError(hass, temp_low, min_temp, max_temp)
        data[climate.ATTR_TARGET_TEMP_LOW] = temp_low
        response.add_context_property(
            {
                "name": "lowerSetpoint",
                "namespace": "Alexa.ThermostatController",
                "value": {"value": temp_low, "scale": API_TEMP_UNITS[unit]},
            }
        )
    if "upperSetpoint" in payload:
        temp_high = temperature_from_object(hass, payload["upperSetpoint"])
        if temp_high < min_temp or temp_high > max_temp:
            raise AlexaTempRangeError(hass, temp_high, min_temp, max_temp)
        data[climate.ATTR_TARGET_TEMP_HIGH] = temp_high
        response.add_context_property(
            {
                "name": "upperSetpoint",
                "namespace": "Alexa.ThermostatController",
                "value": {"value": temp_high, "scale": API_TEMP_UNITS[unit]},
            }
        )

    service = SERVICE_SET_TEMPERATURE[domain]

    await hass.services.async_call(
        entity.domain,
        service,
        data,
        blocking=False,
        context=context,
    )

    return response


@HANDLERS.register(("Alexa.ThermostatController", "AdjustTargetTemperature"))
async def async_api_adjust_target_temp(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process an adjust target temperature request for climates and water heaters."""
    data: dict[str, Any]
    entity = directive.entity
    domain = entity.domain
    min_temp = entity.attributes[MIN_MAX_TEMP[domain]["min_temp"]]
    max_temp = entity.attributes[MIN_MAX_TEMP[domain]["max_temp"]]
    unit = hass.config.units.temperature_unit

    temp_delta = temperature_from_object(
        hass, directive.payload["targetSetpointDelta"], interval=True
    )

    response = directive.response()

    current_target_temp_high = entity.attributes.get(climate.ATTR_TARGET_TEMP_HIGH)
    current_target_temp_low = entity.attributes.get(climate.ATTR_TARGET_TEMP_LOW)
    if current_target_temp_high is not None and current_target_temp_low is not None:
        target_temp_high = float(current_target_temp_high) + temp_delta
        if target_temp_high < min_temp or target_temp_high > max_temp:
            raise AlexaTempRangeError(hass, target_temp_high, min_temp, max_temp)

        target_temp_low = float(current_target_temp_low) + temp_delta
        if target_temp_low < min_temp or target_temp_low > max_temp:
            raise AlexaTempRangeError(hass, target_temp_low, min_temp, max_temp)

        data = {
            ATTR_ENTITY_ID: entity.entity_id,
            climate.ATTR_TARGET_TEMP_HIGH: target_temp_high,
            climate.ATTR_TARGET_TEMP_LOW: target_temp_low,
        }

        response.add_context_property(
            {
                "name": "upperSetpoint",
                "namespace": "Alexa.ThermostatController",
                "value": {"value": target_temp_high, "scale": API_TEMP_UNITS[unit]},
            }
        )
        response.add_context_property(
            {
                "name": "lowerSetpoint",
                "namespace": "Alexa.ThermostatController",
                "value": {"value": target_temp_low, "scale": API_TEMP_UNITS[unit]},
            }
        )
    else:
        current_target_temp: str | None = entity.attributes.get(ATTR_TEMPERATURE)
        if current_target_temp is None:
            raise AlexaUnsupportedThermostatTargetStateError(
                "The current target temperature is not set, "
                "cannot adjust target temperature"
            )
        target_temp = float(current_target_temp) + temp_delta

        if target_temp < min_temp or target_temp > max_temp:
            raise AlexaTempRangeError(hass, target_temp, min_temp, max_temp)

        data = {ATTR_ENTITY_ID: entity.entity_id, ATTR_TEMPERATURE: target_temp}
        response.add_context_property(
            {
                "name": "targetSetpoint",
                "namespace": "Alexa.ThermostatController",
                "value": {"value": target_temp, "scale": API_TEMP_UNITS[unit]},
            }
        )

    service = SERVICE_SET_TEMPERATURE[domain]

    await hass.services.async_call(
        entity.domain,
        service,
        data,
        blocking=False,
        context=context,
    )

    return response


@HANDLERS.register(("Alexa.ThermostatController", "SetThermostatMode"))
async def async_api_set_thermostat_mode(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a set thermostat mode request."""
    operation_list: list[str]

    entity = directive.entity
    mode = directive.payload["thermostatMode"]
    mode = mode if isinstance(mode, str) else mode["value"]

    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    ha_preset = next((k for k, v in API_THERMOSTAT_PRESETS.items() if v == mode), None)

    if ha_preset:
        presets = entity.attributes.get(climate.ATTR_PRESET_MODES) or []

        if ha_preset not in presets:
            msg = f"The requested thermostat mode {ha_preset} is not supported"
            raise AlexaUnsupportedThermostatModeError(msg)

        service = climate.SERVICE_SET_PRESET_MODE
        data[climate.ATTR_PRESET_MODE] = ha_preset

    elif mode == "CUSTOM":
        operation_list = entity.attributes.get(climate.ATTR_HVAC_MODES) or []
        custom_mode = directive.payload["thermostatMode"]["customName"]
        custom_mode = next(
            (k for k, v in API_THERMOSTAT_MODES_CUSTOM.items() if v == custom_mode),
            None,
        )
        if custom_mode not in operation_list:
            msg = (
                f"The requested thermostat mode {mode}: {custom_mode} is not supported"
            )
            raise AlexaUnsupportedThermostatModeError(msg)

        service = climate.SERVICE_SET_HVAC_MODE
        data[climate.ATTR_HVAC_MODE] = custom_mode

    else:
        operation_list = entity.attributes.get(climate.ATTR_HVAC_MODES) or []
        ha_modes: dict[str, str] = {
            k: v for k, v in API_THERMOSTAT_MODES.items() if v == mode
        }
        ha_mode: str | None = next(
            iter(set(ha_modes).intersection(operation_list)), None
        )
        if ha_mode not in operation_list:
            msg = f"The requested thermostat mode {mode} is not supported"
            raise AlexaUnsupportedThermostatModeError(msg)

        service = climate.SERVICE_SET_HVAC_MODE
        data[climate.ATTR_HVAC_MODE] = ha_mode

    response = directive.response()
    await hass.services.async_call(
        climate.DOMAIN, service, data, blocking=False, context=context
    )
    response.add_context_property(
        {
            "name": "thermostatMode",
            "namespace": "Alexa.ThermostatController",
            "value": mode,
        }
    )

    return response


@HANDLERS.register(("Alexa", "ReportState"))
async def async_api_reportstate(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a ReportState request."""
    return directive.response(name="StateReport")


@HANDLERS.register(("Alexa.SecurityPanelController", "Arm"))
async def async_api_arm(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a Security Panel Arm request."""
    entity = directive.entity
    service = None
    arm_state = directive.payload["armState"]
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    if entity.state != STATE_ALARM_DISARMED:
        msg = "You must disarm the system before you can set the requested arm state."
        raise AlexaSecurityPanelAuthorizationRequired(msg)

    if arm_state == "ARMED_AWAY":
        service = SERVICE_ALARM_ARM_AWAY
    elif arm_state == "ARMED_NIGHT":
        service = SERVICE_ALARM_ARM_NIGHT
    elif arm_state == "ARMED_STAY":
        service = SERVICE_ALARM_ARM_HOME
    else:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        entity.domain, service, data, blocking=False, context=context
    )

    # return 0 until alarm integration supports an exit delay
    payload: dict[str, Any] = {"exitDelayInSeconds": 0}

    response = directive.response(
        name="Arm.Response", namespace="Alexa.SecurityPanelController", payload=payload
    )

    response.add_context_property(
        {
            "name": "armState",
            "namespace": "Alexa.SecurityPanelController",
            "value": arm_state,
        }
    )

    return response


@HANDLERS.register(("Alexa.SecurityPanelController", "Disarm"))
async def async_api_disarm(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a Security Panel Disarm request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}
    response = directive.response()

    # Per Alexa Documentation: If you receive a Disarm directive, and the
    # system is already disarmed, respond with a success response,
    # not an error response.
    if entity.state == STATE_ALARM_DISARMED:
        return response

    payload = directive.payload
    if "authorization" in payload:
        value = payload["authorization"]["value"]
        if payload["authorization"]["type"] == "FOUR_DIGIT_PIN":
            data["code"] = value

    await hass.services.async_call(
        entity.domain, SERVICE_ALARM_DISARM, data, blocking=True, context=context
    )

    response.add_context_property(
        {
            "name": "armState",
            "namespace": "Alexa.SecurityPanelController",
            "value": "DISARMED",
        }
    )

    return response


@HANDLERS.register(("Alexa.ModeController", "SetMode"))
async def async_api_set_mode(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a SetMode directive."""
    entity = directive.entity
    instance = directive.instance
    domain = entity.domain
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}
    mode = directive.payload["mode"]
    if instance is None:
        raise ValueError("Instance cannot be None")

    service, data = determine_service_and_data(entity, instance, mode, data)

    if not service:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        domain, service, data, blocking=False, context=context
    )

    response = directive.response()
    response.add_context_property(
        {
            "namespace": "Alexa.ModeController",
            "instance": instance,
            "name": "mode",
            "value": mode,
        }
    )

    return response


def determine_service_and_data(
    entity: ha.State, instance: str, mode: str, data: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """Determine the appropriate service and data for the given entity, instance, and mode."""
    # Ensure mode is in the correct format and can be split
    if not isinstance(mode, str) or "." not in mode:
        raise ValueError(f"Invalid mode format: {mode}")
    # Safely split the mode
    mode_split = mode.split(".")[1] if mode and "." in mode else None
    if not mode_split:
        raise ValueError(f"Unable to split mode: {mode}")

    domain_instance_mapping = {
        fan.DOMAIN: {
            fan.ATTR_DIRECTION: (
                fan.SERVICE_SET_DIRECTION,
                fan.ATTR_DIRECTION,
                [fan.DIRECTION_REVERSE, fan.DIRECTION_FORWARD],
            ),
            fan.ATTR_PRESET_MODE: (
                fan.SERVICE_SET_PRESET_MODE,
                fan.ATTR_PRESET_MODE,
                entity.attributes.get(fan.ATTR_PRESET_MODES) or [],
            ),
        },
        humidifier.DOMAIN: {
            humidifier.ATTR_MODE: (
                humidifier.SERVICE_SET_MODE,
                humidifier.ATTR_MODE,
                entity.attributes.get(humidifier.ATTR_AVAILABLE_MODES) or [],
            ),
        },
        remote.DOMAIN: {
            remote.ATTR_ACTIVITY: (
                remote.SERVICE_TURN_ON,
                remote.ATTR_ACTIVITY,
                entity.attributes.get(remote.ATTR_ACTIVITY_LIST) or [],
            ),
        },
        water_heater.DOMAIN: {
            water_heater.ATTR_OPERATION_MODE: (
                water_heater.SERVICE_SET_OPERATION_MODE,
                water_heater.ATTR_OPERATION_MODE,
                entity.attributes.get(water_heater.ATTR_OPERATION_LIST) or [],
            ),
        },
        cover.DOMAIN: {
            cover.ATTR_POSITION: determine_cover_service(mode_split),
        },
        valve.DOMAIN: {
            "state": determine_valve_service(mode_split),
        },
    }

    # Ensure that the entity domain is valid and the instance exists in the mapping
    domain_mapping = domain_instance_mapping.get(entity.domain)
    if domain_mapping is None:
        raise ValueError(f"Domain '{entity.domain}' is not supported")

    service_data = domain_mapping.get(instance)  # type: ignore[attr-defined]
    if service_data is None:
        raise TypeError(f"Instance '{instance}' not found in domain '{entity.domain}'")

    # Check if the service_data is a tuple and process it accordingly
    if isinstance(service_data, tuple):
        service, attr, valid_modes = service_data

        if not isinstance(valid_modes, list):
            raise TypeError(f"Expected a list of valid modes, got {type(valid_modes)}")

        if mode_split not in valid_modes:
            raise AlexaInvalidValueError(
                f"Entity '{entity.entity_id}' does not support Mode '{mode_split}'"
            )

        data[attr] = mode_split
        return service, data

    # If service_data is not a tuple, return it directly
    return service_data, data


def determine_cover_service(position: str) -> str | None:
    """Determine the appropriate cover service based on the cover's position."""
    if position == cover.STATE_CLOSED:
        return cover.SERVICE_CLOSE_COVER
    if position == cover.STATE_OPEN:
        return cover.SERVICE_OPEN_COVER
    if position == "custom":
        return cover.SERVICE_STOP_COVER
    return None


def determine_valve_service(position: str) -> str | None:
    """Determine the appropriate valve service based on the valve's position."""
    if position == valve.STATE_CLOSED:
        return valve.SERVICE_CLOSE_VALVE
    if position == valve.STATE_OPEN:
        return valve.SERVICE_OPEN_VALVE
    return None


@HANDLERS.register(("Alexa.ModeController", "AdjustMode"))
async def async_api_adjust_mode(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a AdjustMode request.

    Requires capabilityResources supportedModes to be ordered.
    Only supportedModes with ordered=True support the adjustMode directive.
    """

    # Currently no supportedModes are configured with ordered=True
    # to support this request.
    raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)


@HANDLERS.register(("Alexa.ToggleController", "TurnOn"))
async def async_api_toggle_on(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a toggle on request."""
    entity = directive.entity
    instance = directive.instance
    domain = entity.domain

    data: dict[str, Any]

    # Fan Oscillating
    if instance == f"{fan.DOMAIN}.{fan.ATTR_OSCILLATING}":
        service = fan.SERVICE_OSCILLATE
        data = {
            ATTR_ENTITY_ID: entity.entity_id,
            fan.ATTR_OSCILLATING: True,
        }
    elif instance == f"{valve.DOMAIN}.stop":
        service = valve.SERVICE_STOP_VALVE
        data = {
            ATTR_ENTITY_ID: entity.entity_id,
        }
    else:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        domain, service, data, blocking=False, context=context
    )

    response = directive.response()
    response.add_context_property(
        {
            "namespace": "Alexa.ToggleController",
            "instance": instance,
            "name": "toggleState",
            "value": "ON",
        }
    )

    return response


@HANDLERS.register(("Alexa.ToggleController", "TurnOff"))
async def async_api_toggle_off(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a toggle off request."""
    entity = directive.entity
    instance = directive.instance
    domain = entity.domain

    # Fan Oscillating
    if instance != f"{fan.DOMAIN}.{fan.ATTR_OSCILLATING}":
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    service = fan.SERVICE_OSCILLATE
    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        fan.ATTR_OSCILLATING: False,
    }

    await hass.services.async_call(
        domain, service, data, blocking=False, context=context
    )

    response = directive.response()
    response.add_context_property(
        {
            "namespace": "Alexa.ToggleController",
            "instance": instance,
            "name": "toggleState",
            "value": "OFF",
        }
    )

    return response


@HANDLERS.register(("Alexa.RangeController", "SetRangeValue"))
async def async_api_set_range(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a next request."""
    entity = directive.entity
    instance = directive.instance
    domain = entity.domain
    service = None
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}
    range_value = directive.payload["rangeValue"]
    supported = entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

    # Cover Position
    if instance == f"{cover.DOMAIN}.{cover.ATTR_POSITION}":
        range_value = int(range_value)
        if supported & cover.CoverEntityFeature.CLOSE and range_value == 0:
            service = cover.SERVICE_CLOSE_COVER
        elif supported & cover.CoverEntityFeature.OPEN and range_value == 100:
            service = cover.SERVICE_OPEN_COVER
        else:
            service = cover.SERVICE_SET_COVER_POSITION
            data[cover.ATTR_POSITION] = range_value

    # Cover Tilt
    elif instance == f"{cover.DOMAIN}.tilt":
        range_value = int(range_value)
        if supported & cover.CoverEntityFeature.CLOSE_TILT and range_value == 0:
            service = cover.SERVICE_CLOSE_COVER_TILT
        elif supported & cover.CoverEntityFeature.OPEN_TILT and range_value == 100:
            service = cover.SERVICE_OPEN_COVER_TILT
        else:
            service = cover.SERVICE_SET_COVER_TILT_POSITION
            data[cover.ATTR_TILT_POSITION] = range_value

    # Fan Speed
    elif instance == f"{fan.DOMAIN}.{fan.ATTR_PERCENTAGE}":
        range_value = int(range_value)
        if range_value == 0:
            service = fan.SERVICE_TURN_OFF
        elif supported & fan.FanEntityFeature.SET_SPEED:
            service = fan.SERVICE_SET_PERCENTAGE
            data[fan.ATTR_PERCENTAGE] = range_value
        else:
            service = fan.SERVICE_TURN_ON

    # Humidifier target humidity
    elif instance == f"{humidifier.DOMAIN}.{humidifier.ATTR_HUMIDITY}":
        range_value = int(range_value)
        service = humidifier.SERVICE_SET_HUMIDITY
        data[humidifier.ATTR_HUMIDITY] = range_value

    # Input Number Value
    elif instance == f"{input_number.DOMAIN}.{input_number.ATTR_VALUE}":
        range_value = float(range_value)
        service = input_number.SERVICE_SET_VALUE
        min_value = float(entity.attributes[input_number.ATTR_MIN])
        max_value = float(entity.attributes[input_number.ATTR_MAX])
        data[input_number.ATTR_VALUE] = min(max_value, max(min_value, range_value))

    # Input Number Value
    elif instance == f"{number.DOMAIN}.{number.ATTR_VALUE}":
        range_value = float(range_value)
        service = number.SERVICE_SET_VALUE
        min_value = float(entity.attributes[number.ATTR_MIN])
        max_value = float(entity.attributes[number.ATTR_MAX])
        data[number.ATTR_VALUE] = min(max_value, max(min_value, range_value))

    # Vacuum Fan Speed
    elif instance == f"{vacuum.DOMAIN}.{vacuum.ATTR_FAN_SPEED}":
        service = vacuum.SERVICE_SET_FAN_SPEED
        speed_list = entity.attributes[vacuum.ATTR_FAN_SPEED_LIST]
        speed = next(
            (v for i, v in enumerate(speed_list) if i == int(range_value)), None
        )

        if not speed:
            msg = "Entity does not support value"
            raise AlexaInvalidValueError(msg)

        data[vacuum.ATTR_FAN_SPEED] = speed

    # Valve Position
    elif instance == f"{valve.DOMAIN}.{valve.ATTR_POSITION}":
        range_value = int(range_value)
        if supported & valve.ValveEntityFeature.CLOSE and range_value == 0:
            service = valve.SERVICE_CLOSE_VALVE
        elif supported & valve.ValveEntityFeature.OPEN and range_value == 100:
            service = valve.SERVICE_OPEN_VALVE
        else:
            service = valve.SERVICE_SET_VALVE_POSITION
            data[valve.ATTR_POSITION] = range_value

    else:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        domain, service, data, blocking=False, context=context
    )

    response = directive.response()
    response.add_context_property(
        {
            "namespace": "Alexa.RangeController",
            "instance": instance,
            "name": "rangeValue",
            "value": range_value,
        }
    )

    return response


@HANDLERS.register(("Alexa.RangeController", "AdjustRangeValue"))
async def async_api_adjust_range(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a next request."""
    entity = directive.entity
    instance = directive.instance
    domain = entity.domain
    service = None
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}
    range_delta = directive.payload["rangeValueDelta"]
    range_delta_default = bool(directive.payload["rangeValueDeltaDefault"])
    response_value: int | None = 0

    # Cover Position
    if instance == f"{cover.DOMAIN}.{cover.ATTR_POSITION}":
        range_delta = int(range_delta * 20) if range_delta_default else int(range_delta)
        service = SERVICE_SET_COVER_POSITION
        if not (current := entity.attributes.get(cover.ATTR_CURRENT_POSITION)):
            msg = f"Unable to determine {entity.entity_id} current position"
            raise AlexaInvalidValueError(msg)
        position = response_value = min(100, max(0, range_delta + current))
        if position == 100:
            service = cover.SERVICE_OPEN_COVER
        elif position == 0:
            service = cover.SERVICE_CLOSE_COVER
        else:
            data[cover.ATTR_POSITION] = position

    # Cover Tilt
    elif instance == f"{cover.DOMAIN}.tilt":
        range_delta = int(range_delta * 20) if range_delta_default else int(range_delta)
        service = SERVICE_SET_COVER_TILT_POSITION
        current = entity.attributes.get(cover.ATTR_TILT_POSITION)
        if not current:
            msg = f"Unable to determine {entity.entity_id} current tilt position"
            raise AlexaInvalidValueError(msg)
        tilt_position = response_value = min(100, max(0, range_delta + current))
        if tilt_position == 100:
            service = cover.SERVICE_OPEN_COVER_TILT
        elif tilt_position == 0:
            service = cover.SERVICE_CLOSE_COVER_TILT
        else:
            data[cover.ATTR_TILT_POSITION] = tilt_position

    # Fan speed percentage
    elif instance == f"{fan.DOMAIN}.{fan.ATTR_PERCENTAGE}":
        percentage_step = entity.attributes.get(fan.ATTR_PERCENTAGE_STEP) or 20
        range_delta = (
            int(range_delta * percentage_step)
            if range_delta_default
            else int(range_delta)
        )
        service = fan.SERVICE_SET_PERCENTAGE
        if not (current := entity.attributes.get(fan.ATTR_PERCENTAGE)):
            msg = f"Unable to determine {entity.entity_id} current fan speed"
            raise AlexaInvalidValueError(msg)
        percentage = response_value = min(100, max(0, range_delta + current))
        if percentage:
            data[fan.ATTR_PERCENTAGE] = percentage
        else:
            service = fan.SERVICE_TURN_OFF

    # Humidifier target humidity
    elif instance == f"{humidifier.DOMAIN}.{humidifier.ATTR_HUMIDITY}":
        percentage_step = 5
        range_delta = (
            int(range_delta * percentage_step)
            if range_delta_default
            else int(range_delta)
        )
        service = humidifier.SERVICE_SET_HUMIDITY
        if not (current := entity.attributes.get(humidifier.ATTR_HUMIDITY)):
            msg = f"Unable to determine {entity.entity_id} current target humidity"
            raise AlexaInvalidValueError(msg)
        min_value = entity.attributes.get(humidifier.ATTR_MIN_HUMIDITY, 10)
        max_value = entity.attributes.get(humidifier.ATTR_MAX_HUMIDITY, 90)
        percentage = response_value = min(
            max_value, max(min_value, range_delta + current)
        )
        if percentage:
            data[humidifier.ATTR_HUMIDITY] = percentage

    # Input Number Value
    elif instance == f"{input_number.DOMAIN}.{input_number.ATTR_VALUE}":
        range_delta = float(range_delta)
        service = input_number.SERVICE_SET_VALUE
        min_value = float(entity.attributes[input_number.ATTR_MIN])
        max_value = float(entity.attributes[input_number.ATTR_MAX])
        current = float(entity.state)
        data[input_number.ATTR_VALUE] = response_value = min(
            max_value, max(min_value, range_delta + current)
        )

    # Number Value
    elif instance == f"{number.DOMAIN}.{number.ATTR_VALUE}":
        range_delta = float(range_delta)
        service = number.SERVICE_SET_VALUE
        min_value = float(entity.attributes[number.ATTR_MIN])
        max_value = float(entity.attributes[number.ATTR_MAX])
        current = float(entity.state)
        data[number.ATTR_VALUE] = response_value = min(
            max_value, max(min_value, range_delta + current)
        )

    # Vacuum Fan Speed
    elif instance == f"{vacuum.DOMAIN}.{vacuum.ATTR_FAN_SPEED}":
        range_delta = int(range_delta)
        service = vacuum.SERVICE_SET_FAN_SPEED
        speed_list = entity.attributes[vacuum.ATTR_FAN_SPEED_LIST]
        current_speed = entity.attributes[vacuum.ATTR_FAN_SPEED]
        current_speed_index = next(
            (i for i, v in enumerate(speed_list) if v == current_speed), 0
        )
        new_speed_index = min(
            len(speed_list) - 1, max(0, current_speed_index + range_delta)
        )
        speed = next(
            (v for i, v in enumerate(speed_list) if i == new_speed_index), None
        )
        data[vacuum.ATTR_FAN_SPEED] = response_value = speed

    # Valve Position
    elif instance == f"{valve.DOMAIN}.{valve.ATTR_POSITION}":
        range_delta = int(range_delta * 20) if range_delta_default else int(range_delta)
        service = valve.SERVICE_SET_VALVE_POSITION
        if not (current := entity.attributes.get(valve.ATTR_POSITION)):
            msg = f"Unable to determine {entity.entity_id} current position"
            raise AlexaInvalidValueError(msg)
        position = response_value = min(100, max(0, range_delta + current))
        if position == 100:
            service = valve.SERVICE_OPEN_VALVE
        elif position == 0:
            service = valve.SERVICE_CLOSE_VALVE
        else:
            data[valve.ATTR_POSITION] = position

    else:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        domain, service, data, blocking=False, context=context
    )

    response = directive.response()
    response.add_context_property(
        {
            "namespace": "Alexa.RangeController",
            "instance": instance,
            "name": "rangeValue",
            "value": response_value,
        }
    )

    return response


@HANDLERS.register(("Alexa.ChannelController", "ChangeChannel"))
async def async_api_changechannel(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a change channel request."""
    channel = "0"
    entity = directive.entity
    channel_payload = directive.payload["channel"]
    metadata_payload = directive.payload["channelMetadata"]
    payload_name = "number"

    if "number" in channel_payload:
        channel = channel_payload["number"]
        payload_name = "number"
    elif "callSign" in channel_payload:
        channel = channel_payload["callSign"]
        payload_name = "callSign"
    elif "affiliateCallSign" in channel_payload:
        channel = channel_payload["affiliateCallSign"]
        payload_name = "affiliateCallSign"
    elif "uri" in channel_payload:
        channel = channel_payload["uri"]
        payload_name = "uri"
    elif "name" in metadata_payload:
        channel = metadata_payload["name"]
        payload_name = "callSign"

    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        media_player.const.ATTR_MEDIA_CONTENT_ID: channel,
        media_player.const.ATTR_MEDIA_CONTENT_TYPE: (
            media_player.const.MEDIA_TYPE_CHANNEL
        ),
    }

    await hass.services.async_call(
        entity.domain,
        media_player.const.SERVICE_PLAY_MEDIA,
        data,
        blocking=False,
        context=context,
    )

    response = directive.response()

    response.add_context_property(
        {
            "namespace": "Alexa.ChannelController",
            "name": "channel",
            "value": {payload_name: channel},
        }
    )

    return response


@HANDLERS.register(("Alexa.ChannelController", "SkipChannels"))
async def async_api_skipchannel(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a skipchannel request."""
    channel = int(directive.payload["channelCount"])
    entity = directive.entity

    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    if channel < 0:
        service_media = SERVICE_MEDIA_PREVIOUS_TRACK
    else:
        service_media = SERVICE_MEDIA_NEXT_TRACK

    for _ in range(abs(channel)):
        await hass.services.async_call(
            entity.domain, service_media, data, blocking=False, context=context
        )

    response = directive.response()

    response.add_context_property(
        {
            "namespace": "Alexa.ChannelController",
            "name": "channel",
            "value": {"number": ""},
        }
    )

    return response


@HANDLERS.register(("Alexa.SeekController", "AdjustSeekPosition"))
async def async_api_seek(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a seek request."""
    entity = directive.entity
    position_delta = int(directive.payload["deltaPositionMilliseconds"])

    current_position = entity.attributes.get(media_player.ATTR_MEDIA_POSITION)
    if not current_position:
        msg = f"{entity} did not return the current media position."
        raise AlexaVideoActionNotPermittedForContentError(msg)

    seek_position = max(int(current_position) + int(position_delta / 1000), 0)

    media_duration = entity.attributes.get(media_player.ATTR_MEDIA_DURATION)
    if media_duration and 0 < int(media_duration) < seek_position:
        seek_position = media_duration

    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity.entity_id,
        media_player.ATTR_MEDIA_SEEK_POSITION: seek_position,
    }

    await hass.services.async_call(
        media_player.DOMAIN,
        media_player.SERVICE_MEDIA_SEEK,
        data,
        blocking=False,
        context=context,
    )

    # convert seconds to milliseconds for StateReport.
    seek_position = int(seek_position * 1000)

    payload: dict[str, Any] = {
        "properties": [{"name": "positionMilliseconds", "value": seek_position}]
    }
    return directive.response(
        name="StateReport", namespace="Alexa.SeekController", payload=payload
    )


@HANDLERS.register(("Alexa.EqualizerController", "SetMode"))
async def async_api_set_eq_mode(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a SetMode request for EqualizerController."""
    mode = directive.payload["mode"]
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    sound_mode_list = entity.attributes.get(media_player.const.ATTR_SOUND_MODE_LIST)
    if sound_mode_list and mode.lower() in sound_mode_list:
        data[media_player.const.ATTR_SOUND_MODE] = mode.lower()
    else:
        msg = f"failed to map sound mode {mode} to a mode on {entity.entity_id}"
        raise AlexaInvalidValueError(msg)

    await hass.services.async_call(
        entity.domain,
        media_player.SERVICE_SELECT_SOUND_MODE,
        data,
        blocking=False,
        context=context,
    )

    return directive.response()


@HANDLERS.register(("Alexa.EqualizerController", "AdjustBands"))
@HANDLERS.register(("Alexa.EqualizerController", "ResetBands"))
@HANDLERS.register(("Alexa.EqualizerController", "SetBands"))
async def async_api_bands_directive(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Handle an AdjustBands, ResetBands, SetBands request.

    Only mode directives are currently supported for the EqualizerController.
    """
    # Currently bands directives are not supported.
    raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)


@HANDLERS.register(("Alexa.TimeHoldController", "Hold"))
async def async_api_hold(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a TimeHoldController Hold request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    if entity.domain == timer.DOMAIN:
        service = timer.SERVICE_PAUSE

    elif entity.domain == vacuum.DOMAIN:
        service = vacuum.SERVICE_START_PAUSE

    else:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        entity.domain, service, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.TimeHoldController", "Resume"))
async def async_api_resume(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a TimeHoldController Resume request."""
    entity = directive.entity
    data: dict[str, Any] = {ATTR_ENTITY_ID: entity.entity_id}

    if entity.domain == timer.DOMAIN:
        service = timer.SERVICE_START

    elif entity.domain == vacuum.DOMAIN:
        service = vacuum.SERVICE_START_PAUSE

    else:
        raise AlexaInvalidDirectiveError(DIRECTIVE_NOT_SUPPORTED)

    await hass.services.async_call(
        entity.domain, service, data, blocking=False, context=context
    )

    return directive.response()


@HANDLERS.register(("Alexa.CameraStreamController", "InitializeCameraStreams"))
async def async_api_initialize_camera_stream(
    hass: ha.HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: ha.Context,
) -> AlexaResponse:
    """Process a InitializeCameraStreams request."""
    entity = directive.entity
    stream_source = await camera.async_request_stream(hass, entity.entity_id, fmt="hls")
    state = hass.states.get(entity.entity_id)
    assert state
    camera_image = state.attributes[ATTR_ENTITY_PICTURE]

    try:
        external_url = network.get_url(
            hass,
            allow_internal=False,
            allow_ip=False,
            require_ssl=True,
            require_standard_port=True,
        )
    except network.NoURLAvailableError as err:
        raise AlexaInvalidValueError(
            "Failed to find suitable URL to serve to Alexa"
        ) from err

    payload: dict[str, Any] = {
        "cameraStreams": [
            {
                "uri": f"{external_url}{stream_source}",
                "protocol": "HLS",
                "resolution": {"width": 1280, "height": 720},
                "authorizationType": "NONE",
                "videoCodec": "H264",
                "audioCodec": "AAC",
            }
        ],
        "imageUri": f"{external_url}{camera_image}",
    }
    return directive.response(
        name="Response", namespace="Alexa.CameraStreamController", payload=payload
    )
