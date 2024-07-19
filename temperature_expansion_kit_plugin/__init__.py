# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from time import sleep
from typing import cast
from typing import Optional

import board
import click
import digitalio
from msgspec.json import decode
from pioreactor import error_codes
from pioreactor import exc
from pioreactor import hardware
from pioreactor import structs
from pioreactor import types as pt
from pioreactor.automations.base import AutomationJob
from pioreactor.config import config
from pioreactor.logging import create_logger
from pioreactor.structs import Temperature
from pioreactor.utils import clamp
from pioreactor.utils import is_pio_job_running
from pioreactor.utils import local_intermittent_storage
from pioreactor.utils import whoami
from pioreactor.utils.pwm import PWM
from pioreactor.utils.streaming_calculations import PID
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.utils.timing import current_utc_timestamp
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.utils.timing import to_datetime

try:
    from .max31865 import MAX31865
except ImportError:
    # if using in the plugins folder, we don't use . else we get
    # "attempted relative import with no known parent package"
    from max31865 import MAX31865  # type: ignore


class TemperatureAutomationJobWithProbe(AutomationJob):
    """
    This is the super class that Temperature automations inherit from.
    The `execute` function, which is what subclasses will define, is updated every time a new temperature is computed.
    Temperatures are updated every `INFERENCE_EVERY_N_SECONDS` seconds.

    To change setting over MQTT:

    `pioreactor/<unit>/<experiment>/temperature_automation/<setting>/set` value

    """

    INFERENCE_SAMPLES_EVERY_T_SECONDS: float = 5.0

    if whoami.get_pioreactor_version() == (1, 0):
        # made from PLA
        MAX_TEMP_TO_REDUCE_HEATING = 63.0
        MAX_TEMP_TO_DISABLE_HEATING = 65.0  # probably okay, but can't stay here for too long
        MAX_TEMP_TO_SHUTDOWN = 66.0
        INFERENCE_EVERY_N_SECONDS: float = 3

    elif whoami.get_pioreactor_version() >= (1, 1):
        # made from PC-CF
        MAX_TEMP_TO_REDUCE_HEATING = 78.0
        MAX_TEMP_TO_DISABLE_HEATING = 80.0
        MAX_TEMP_TO_SHUTDOWN = 85.0  # risk damaging PCB components
        INFERENCE_EVERY_N_SECONDS = 3

    _latest_growth_rate: Optional[float] = None
    _latest_normalized_od: Optional[float] = None
    previous_normalized_od: Optional[float] = None
    previous_growth_rate: Optional[float] = None

    latest_temperature = None
    previous_temperature = None

    automation_name = "temperature_automation_base"  # is overwritten in subclasses
    job_name = "temperature_automation"

    published_settings: dict[str, pt.PublishableSetting] = {
        "temperature": {"datatype": "Temperature", "settable": False, "unit": "℃"},
        "heater_duty_cycle": {"datatype": "float", "settable": False, "unit": "%"},
    }

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # this registers all subclasses of TemperatureAutomationJob
        if (
            hasattr(cls, "automation_name")
            and getattr(cls, "automation_name") != "temperature_automation_base"
        ):
            available_temperature_automations[cls.automation_name] = cls

    def __init__(
        self,
        unit: str,
        experiment: str,
        **kwargs,
    ) -> None:
        super().__init__(unit, experiment)

        self.add_to_published_settings(
            "temperature", {"datatype": "Temperature", "settable": False, "unit": "℃"}
        )

        self.add_to_published_settings(
            "heater_duty_cycle",
            {"datatype": "float", "settable": False, "unit": "%"},
        )

        if not hardware.is_heating_pcb_present():
            self.logger.error("Heating PCB must be attached to Pioreactor HAT")
            self.clean_up()
            raise exc.HardwareNotFoundError("Heating PCB must be attached to Pioreactor HAT")

        if whoami.is_testing_env():
            from pioreactor.utils.mock import MockTMP1075 as TMP1075
        else:
            from pioreactor.utils.temps import TMP1075  # type: ignore

        self.heater_duty_cycle = 0.0
        self.pwm = self.setup_pwm()

        self.heating_pcb_tmp_driver = TMP1075(address=hardware.TEMP)
        self.temperature_probe_driver = self.setup_temperature_probe()

        self.read_external_temperature_timer = RepeatedTimer(
            20,
            self.read_heating_pcb_temperature,
            job_name=self.job_name,
            run_immediately=False,
        ).start()

        self.publish_temperature_timer = RepeatedTimer(
            int(self.INFERENCE_EVERY_N_SECONDS),
            self.infer_culture_temperature,
            job_name=self.job_name,
        ).start()

        self.latest_normalized_od_at: datetime = current_utc_datetime()
        self.latest_growth_rate_at: datetime = current_utc_datetime()
        self.latest_temperture_at: datetime = current_utc_datetime()

    @staticmethod
    def seconds_since_last_active_heating() -> float:
        with local_intermittent_storage("temperature_and_heating") as cache:
            if "last_heating_timestamp" in cache:
                return (
                    current_utc_datetime() - to_datetime(cache["last_heating_timestamp"])
                ).total_seconds()
            else:
                return 1_000_000

    def turn_off_heater(self) -> None:
        self._update_heater(0)
        self.pwm.clean_up()
        # we re-instantiate it as some other process may have messed with the channel.
        self.pwm = self.setup_pwm()
        self._update_heater(0)
        self.pwm.clean_up()

    def update_heater(self, new_duty_cycle: float) -> bool:
        """
        Update heater's duty cycle. This function checks for the PWM lock, and will not
        update if the PWM is locked.

        Returns true if the update was made (eg: no lock), else returns false
        """

        if not self.pwm.is_locked():
            return self._update_heater(new_duty_cycle)
        else:
            return False

    def update_heater_with_delta(self, delta_duty_cycle: float) -> bool:
        """
        Update heater's duty cycle by `delta_duty_cycle` amount. This function checks for the PWM lock, and will not
        update if the PWM is locked.

        Returns true if the update was made (eg: no lock), else returns false
        """
        return self.update_heater(self.heater_duty_cycle + delta_duty_cycle)

    def read_heating_pcb_temperature(self) -> float:
        return self._check_if_exceeds_max_temp(self._read_heating_pcb_temperature())

    def infer_culture_temperature(self) -> None:
        samples = 12
        running_sum = 0.0
        for _ in range(samples):
            _temperature = self.temperature_probe_driver.temperature
            if _temperature < -242.0:
                raise ValueError("Recorded null temperature. Is the Pt1000 probe connected?")

            running_sum += _temperature
            sleep(0.05)

        temperature = running_sum / samples

        try:
            self.temperature = Temperature(
                temperature=round(temperature, 4),
                timestamp=current_utc_datetime(),
            )
            self._set_latest_temperature(self.temperature)

        except Exception as e:
            self.logger.debug(e, exc_info=True)
            self.logger.error(e)

    def read_external_temperature(self) -> float:
        return self._check_if_exceeds_max_temp(self._read_external_temperature())

    def is_heater_pwm_locked(self) -> bool:
        """
        Check if the heater PWM channels is locked
        """
        return self.pwm.is_locked()

    @property
    def most_stale_time(self) -> datetime:
        return min(self.latest_normalized_od_at, self.latest_growth_rate_at)

    @property
    def latest_growth_rate(self) -> float:
        # check if None
        if self._latest_growth_rate is None:
            # this should really only happen on the initialization.
            self.logger.debug("Waiting for OD and growth rate data to arrive")
            if not all(is_pio_job_running(["od_reading", "growth_rate_calculating"])):
                raise exc.JobRequiredError(
                    "`od_reading` and `growth_rate_calculating` should be Ready."
                )

        # check most stale time
        if (current_utc_datetime() - self.most_stale_time).seconds > 5 * 60:
            raise exc.JobRequiredError(
                "readings are too stale (over 5 minutes old) - are `od_reading` and `growth_rate_calculating` running?"
            )

        return cast(float, self._latest_growth_rate)

    @property
    def latest_normalized_od(self) -> float:
        # check if None
        if self._latest_normalized_od is None:
            # this should really only happen on the initialization.
            self.logger.debug("Waiting for OD and growth rate data to arrive")
            if not all(is_pio_job_running(["od_reading", "growth_rate_calculating"])):
                raise exc.JobRequiredError(
                    "`od_reading` and `growth_rate_calculating` should be running."
                )

        # check most stale time
        if (current_utc_datetime() - self.most_stale_time).seconds > 5 * 60:
            raise exc.JobRequiredError(
                "readings are too stale (over 5 minutes old) - are `od_reading` and `growth_rate_calculating` running?"
            )

        return cast(float, self._latest_normalized_od)

    ########## Private & internal methods

    def _read_heating_pcb_temperature(self) -> float:
        """
        Read the current temperature from the onboard heating PCB sensor, in Celsius
        """
        samples = 5
        running_sum = 0.0
        try:
            # check temp is fast, let's do it a few times to reduce variance.
            for _ in range(samples):
                running_sum += self.heating_pcb_tmp_driver.get_temperature()
                sleep(0.05)

        except OSError as e:
            self.logger.debug(e, exc_info=True)
            raise exc.HardwareNotFoundError(
                "Is the Heating PCB attached to the Pioreactor HAT? Unable to find temperature sensor."
            )

        averaged_temp = running_sum / samples
        if averaged_temp == 0.0 and self.automation_name != "only_record_temperature":
            # this is a hardware fluke, not sure why, see #308. We will return something very high to make it shutdown
            # todo: still needed? last observed on  July 18, 2022
            self.logger.error("Temp sensor failure. Switching off. See issue #308")
            self._update_heater(0.0)

        with local_intermittent_storage("temperature_and_heating") as cache:
            cache["heating_pcb_temperature"] = averaged_temp
            cache["heating_pcb_temperature_at"] = current_utc_timestamp()

        return averaged_temp

    def _read_external_temperature(self) -> float:
        """
        Read the current temperature from our sensor, in Celsius
        """
        running_sum, running_count = 0.0, 0
        try:
            # check temp is fast, let's do it a few times to reduce variance.
            for i in range(6):
                running_sum += self.heating_pcb_tmp_driver.get_temperature()
                running_count += 1
                sleep(0.05)

        except OSError as e:
            self.logger.debug(e, exc_info=True)
            raise exc.HardwareNotFoundError(
                "Is the Heating PCB attached to the Pioreactor HAT? Unable to find temperature sensor."
            )

        averaged_temp = running_sum / running_count
        if averaged_temp == 0.0 and self.automation_name != "only_record_temperature":
            # this is a hardware fluke, not sure why, see #308. We will return something very high to make it shutdown
            # todo: still needed? last observed on  July 18, 2022
            self.logger.error("Temp sensor failure. Switching off. See issue #308")
            self._update_heater(0.0)

        with local_intermittent_storage("temperature_and_heating") as cache:
            cache["heating_pcb_temperature"] = averaged_temp
            cache["heating_pcb_temperature_at"] = current_utc_timestamp()

        return averaged_temp

    def _update_heater(self, new_duty_cycle: float) -> bool:
        self.heater_duty_cycle = clamp(0.0, round(float(new_duty_cycle), 2), 100.0)
        self.pwm.change_duty_cycle(self.heater_duty_cycle)

        if self.heater_duty_cycle == 0.0:
            with local_intermittent_storage("temperature_and_heating") as cache:
                cache["last_heating_timestamp"] = current_utc_timestamp()

        return True

    def _check_if_exceeds_max_temp(self, temp: float) -> float:
        if temp > self.MAX_TEMP_TO_SHUTDOWN:
            self.logger.error(
                f"Temperature of heating surface has exceeded {self.MAX_TEMP_TO_SHUTDOWN}℃ - currently {temp}℃. This is beyond our recommendations. Shutting down Raspberry Pi to prevent further problems. Take caution when touching the heating surface and wetware."
            )
            self._update_heater(0)

            self.blink_error_code(error_codes.PCB_TEMPERATURE_TOO_HIGH)

            from subprocess import call

            call("sudo shutdown now --poweroff", shell=True)

        elif temp > self.MAX_TEMP_TO_DISABLE_HEATING:
            self.blink_error_code(error_codes.PCB_TEMPERATURE_TOO_HIGH)

            self.logger.warning(
                f"Temperature of heating surface has exceeded {self.MAX_TEMP_TO_DISABLE_HEATING}℃ - currently {temp}℃. This is beyond our recommendations. The heating PWM channel will be forced to 0. Take caution when touching the heating surface and wetware."
            )

            self._update_heater(0)

        elif temp > self.MAX_TEMP_TO_REDUCE_HEATING:
            self.logger.debug(
                f"Temperature of heating surface has exceeded {self.MAX_TEMP_TO_REDUCE_HEATING}℃ - currently {temp}℃. This is close to our maximum recommended value. The heating PWM channel will be reduced to 90% its current value. Take caution when touching the heating surface and wetware."
            )

            self._update_heater(self.heater_duty_cycle * 0.9)

        return temp

    def on_disconnected(self) -> None:
        with suppress(AttributeError):
            self._update_heater(0)

        with suppress(AttributeError):
            self.read_external_temperature_timer.cancel()
            self.publish_temperature_timer.cancel()

        with suppress(AttributeError):
            self.turn_off_heater()

    def setup_heating_pcb_tmp_sensor(self):
        if whoami.is_testing_env():
            from pioreactor.utils.mock import MockTMP1075 as TMP1075
        else:
            from pioreactor.utils.temps import TMP1075  # type: ignore

        return TMP1075(address=hardware.TEMP)

    def setup_temperature_probe(self) -> MAX31865:
        spi = board.SPI()
        cs = digitalio.DigitalInOut(board.D5)  # Chip select of the MAX31865 board.
        sensor = MAX31865(
            spi,
            cs,
            rtd_nominal=1000.0,
            ref_resistor=4300.0,
            wires=3,
            polarity=1,
            filter_frequency=config.getint(
                "temperature_automation.config", "local_ac_hz", fallback=60
            ),
        )
        return sensor

    def on_sleeping(self) -> None:
        self.publish_temperature_timer.pause()
        self._update_heater(0)

    def on_sleeping_to_ready(self) -> None:
        self.publish_temperature_timer.unpause()

    def setup_pwm(self) -> PWM:
        hertz = 16  # technically this doesn't need to be high: it could even be 1hz. However, we want to smooth it's
        # impact (mainly: current sink), over the second. Ex: imagine freq=1hz, dc=40%, and the pump needs to run for
        # 0.3s. The influence of when the heat is one on the pump can be significant in a power-constrained system.
        pin = hardware.PWM_TO_PIN[hardware.HEATER_PWM_TO_PIN]
        pwm = PWM(pin, hertz, unit=self.unit, experiment=self.experiment)
        pwm.start(0)
        return pwm

    def _set_growth_rate(self, message: pt.MQTTMessage) -> None:
        if not message.payload:
            return

        self.previous_growth_rate = self._latest_growth_rate
        payload = decode(message.payload, type=structs.GrowthRate)
        self._latest_growth_rate = payload.growth_rate
        self.latest_growth_rate_at = payload.timestamp

    def _set_latest_temperature(self, temperature: structs.Temperature) -> None:
        # Note: this doesn't use MQTT data (previously it use to)
        self.previous_temperature = self.latest_temperature
        self.latest_temperature = temperature.temperature
        self.latest_temperature_at = temperature.timestamp

        if self.state == self.READY or self.state == self.INIT:
            self.latest_event = self.execute()

        return

    def _set_OD(self, message: pt.MQTTMessage) -> None:
        if not message.payload:
            return
        self.previous_normalized_od = self._latest_normalized_od
        payload = decode(message.payload, type=structs.ODFiltered)
        self._latest_normalized_od = payload.od_filtered
        self.latest_normalized_od_at = payload.timestamp

    def start_passive_listeners(self) -> None:
        self.subscribe_and_callback(
            self._set_growth_rate,
            f"pioreactor/{self.unit}/{self.experiment}/growth_rate_calculating/growth_rate",
            allow_retained=False,
        )

        self.subscribe_and_callback(
            self._set_OD,
            f"pioreactor/{self.unit}/{self.experiment}/growth_rate_calculating/od_filtered",
            allow_retained=False,
        )


class TemperatureAutomationJobWithProbeContrib(TemperatureAutomationJobWithProbe):
    automation_name: str


available_temperature_automations: dict[str, type[TemperatureAutomationJobWithProbe]] = {}

########## Automations below


class Thermostat(TemperatureAutomationJobWithProbe):
    """
    Uses a PID controller to change the DC% to match a target temperature.
    """

    MAX_TARGET_TEMP = 60.0
    automation_name = "thermostat"
    published_settings = {
        "target_temperature": {"datatype": "float", "unit": "℃", "settable": True}
    }

    def __init__(self, target_temperature: float | str, **kwargs) -> None:
        super().__init__(**kwargs)

        self.logger.debug("Using thermostat from temperature_expansion_kit plugin")

        if target_temperature is None:
            raise ValueError("target_temperature must be set")

        if config.getfloat("temperature_automation.thermostat", "Kp") > 1.0:
            raise ValueError("Kp is too high for this thermostat. Is the configuration correct?")

        self.target_temperature = float(target_temperature)
        self.update_heater(self._determine_initial_duty_to_start(self.target_temperature))

        self.pid = PID(
            Kp=config.getfloat("temperature_automation.thermostat", "Kp"),
            Ki=config.getfloat("temperature_automation.thermostat", "Ki"),
            Kd=config.getfloat("temperature_automation.thermostat", "Kd"),
            setpoint=self.target_temperature,
            unit=self.unit,
            experiment=self.experiment,
            job_name=self.job_name,
            target_name="temperature",
            output_limits=(-2.5, 2.5),  # avoid whiplashing
            derivative_smoothing=0.925,
        )

    @staticmethod
    def _determine_initial_duty_to_start(target_temperature: float) -> float:
        # naive, can be improved with a calibration later. Scale by N%.
        return max(0.4 * (3 / 2 * target_temperature - 39), 0)

    def execute(self):
        while not hasattr(self, "pid"):
            # sometimes when initializing, this execute can run before the subclasses __init__ is resolved.
            pass

        assert self.latest_temperature is not None
        output = self.pid.update(self.latest_temperature, dt=1)
        self.update_heater_with_delta(output)

        return

    def set_target_temperature(self, target_temperature: float) -> None:
        """

        Parameters
        ------------

        target_temperature: float
            the new target temperature

        """
        target_temperature = float(target_temperature)
        if target_temperature > self.MAX_TARGET_TEMP:
            self.logger.warning(
                f"Values over {self.MAX_TARGET_TEMP}℃ are not supported. Setting to {self.MAX_TARGET_TEMP}℃."
            )

        target_temperature = clamp(0, target_temperature, self.MAX_TARGET_TEMP)
        self.target_temperature = target_temperature
        self.pid.set_setpoint(self.target_temperature)


def start_temperature_automation(
    automation_name: str,
    unit: Optional[str] = None,
    experiment: Optional[str] = None,
    **kwargs,
) -> TemperatureAutomationJobWithProbe:
    unit = unit or whoami.get_unit_name()
    experiment = experiment or whoami.get_assigned_experiment_name(unit)
    try:
        klass = available_temperature_automations[automation_name]
    except KeyError:
        raise KeyError(
            f"Unable to find {automation_name}. Available automations are {list( available_temperature_automations.keys())}"
        )

    if "skip_first_run" in kwargs:
        del kwargs["skip_first_run"]

    try:
        return klass(
            unit=unit,
            experiment=experiment,
            automation_name=automation_name,
            **kwargs,
        )

    except Exception as e:
        logger = create_logger("temperature_automation")
        logger.error(f"Error: {e}")
        logger.debug(e, exc_info=True)
        raise e


@click.command(
    name="temperature_automation",
    context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
)
@click.option(
    "--automation-name",
    help="set the automation of the system: silent, etc.",
    show_default=True,
    required=True,
)
@click.pass_context
def click_temperature_automation(ctx, automation_name):
    """
    Start an Temperature automation
    """
    la = start_temperature_automation(
        automation_name=automation_name,
        **{ctx.args[i][2:].replace("-", "_"): ctx.args[i + 1] for i in range(0, len(ctx.args), 2)},
    )

    la.block_until_disconnected()
