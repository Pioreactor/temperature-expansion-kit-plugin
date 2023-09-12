# -*- coding: utf-8 -*-
"""
Continuously monitor the bioreactor's temperature and take action. This is the core of the temperature automation.

To change the automation over MQTT,

topic: `pioreactor/<unit>/<experiment>/temperture_control/automation/set`
message: a json object with required keyword arguments, see structs.TemperatureAutomation

"""
from __future__ import annotations

from contextlib import suppress
from time import sleep
from typing import Any
from typing import Optional

import board
import click
import digitalio
from pioreactor import error_codes
from pioreactor import exc
from pioreactor import hardware
from pioreactor import whoami
from pioreactor.automations import BaseAutomationJob
from pioreactor.automations.temperature.base import TemperatureAutomationJob
from pioreactor.background_jobs.temperature_control import TemperatureController
from pioreactor.config import config
from pioreactor.structs import Temperature
from pioreactor.structs import TemperatureAutomation
from pioreactor.utils import clamp
from pioreactor.utils import local_intermittent_storage
from pioreactor.utils.pwm import PWM
from pioreactor.utils.streaming_calculations import PID
from pioreactor.utils.timing import current_utc_datetime
from pioreactor.utils.timing import current_utc_timestamp
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.utils.timing import to_datetime

from .max31865 import MAX31865


class Thermostat(TemperatureAutomationJob):
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
        assert target_temperature is not None, "target_temperature must be set"
        self.target_temperature = float(target_temperature)

        assert (
            config.getfloat("temperature_automation.thermostat", "Kp") <= 1.0
        ), "Kp is too high for this thermostat. Is the configuration correct?"

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


class TemperatureControllerWithProbe(BaseAutomationJob):
    """

    This job publishes to

        pioreactor/<unit>/<experiment>/temperature_control/temperature

    the following:

        {
            "temperature": <float>,
            "timestamp": <ISO 8601 timestamp>
        }

    Parameters
    ------------

    """

    MAX_TEMP_TO_REDUCE_HEATING = (
        63.0  # ~PLA glass transition temp, and I've gone safely above this an it's not a problem.
    )
    MAX_TEMP_TO_DISABLE_HEATING = 65.0  # okay in bursts
    MAX_TEMP_TO_SHUTDOWN = 66.0

    job_name = "temperature_control"

    available_automations = TemperatureController.available_automations

    published_settings = {
        "automation": {"datatype": "Automation", "settable": True},
        "automation_name": {"datatype": "string", "settable": False},
        "temperature": {"datatype": "Temperature", "settable": False, "unit": "℃"},
        "heater_duty_cycle": {"datatype": "float", "settable": False, "unit": "%"},
    }

    def __init__(
        self,
        unit: str,
        experiment: str,
        automation_name: str,
        **kwargs,
    ) -> None:
        super().__init__(unit=unit, experiment=experiment)

        if not hardware.is_HAT_present():
            self.logger.error("Pioreactor HAT must be present.")
            self.clean_up()
            raise exc.HardwareNotFoundError("Pioreactor HAT must be present.")

        if not hardware.is_heating_pcb_present():
            self.logger.error("Heating PCB must be attached to Pioreactor HAT")
            self.clean_up()
            raise exc.HardwareNotFoundError("Heating PCB must be attached to Pioreactor HAT")

        self.pwm = self.setup_pwm()
        self.update_heater(0)

        self.heating_pcb_tmp_driver = self.setup_heating_pcb_tmp_sensor()
        self.temperature_probe_driver = self.setup_temperature_probe()

        self.read_external_temperature_timer = RepeatedTimer(
            20,
            self.read_heating_pcb_temperature,
            run_immediately=False,
        ).start()

        self.publish_temperature_timer = RepeatedTimer(
            3,
            self.infer_culture_temperature,
        ).start()

        try:
            automation_class = self.available_automations[automation_name]
        except KeyError:
            raise KeyError(
                f"Unable to find automation {automation_name}. Available automations are {list(self.available_automations.keys())}"
            )

        self.automation = TemperatureAutomation(automation_name=automation_name, args=kwargs)
        self.logger.info(f"Starting {self.automation}.")
        try:
            self.automation_job = automation_class(
                unit=self.unit,
                experiment=self.experiment,
                temperature_control_parent=self,
                **kwargs,
            )
        except Exception as e:
            self.logger.error(e)
            self.logger.debug(e, exc_info=True)
            self.clean_up()
            raise e
        self.automation_name = self.automation.automation_name

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
        self.pwm.cleanup()
        # we re-instantiate it as some other process may have messed with the channel.
        self.pwm = self.setup_pwm()
        self._update_heater(0)
        self.pwm.cleanup()

    def update_heater(self, new_duty_cycle: float) -> bool:
        """
        Update heater's duty cycle. This function checks for the PWM lock, and will not
        update if the PWM is locked.

        Returns true if the update was made (eg: no lock), else returns false
        """

        if not self.pwm.is_locked():
            return self._update_heater(clamp(0.0, new_duty_cycle, 100.0))
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

    ##### internal and private methods ########

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
            self.set_automation(TemperatureAutomation(automation_name="only_record_temperature"))

        with local_intermittent_storage("temperature_and_heating") as cache:
            cache["heating_pcb_temperature"] = averaged_temp
            cache["heating_pcb_temperature_at"] = current_utc_timestamp()

        return averaged_temp

    def set_automation(self, algo_metadata: TemperatureAutomation) -> None:
        # TODO: this needs a better rollback. Ex: in except, something like
        # self.automation_job.set_state("init")
        # self.automation_job.set_state("ready")
        # OR should just bail...

        assert isinstance(algo_metadata, TemperatureAutomation)

        # users sometimes take the "wrong path" and create a _new_ Thermostat with target_temperature=X
        # instead of just changing the target_temperature in their current Thermostat. We check for this condition,
        # and do the "right" thing for them.
        if (algo_metadata.automation_name == "thermostat") and (
            self.automation.automation_name == "thermostat"
        ):
            # just update the setting, and return
            self.logger.debug(
                "Bypassing changing automations, and just updating the setting on the existing Thermostat automation."
            )
            self.automation_job.set_target_temperature(
                float(algo_metadata.args["target_temperature"])
            )
            self.automation = algo_metadata
            return

        try:
            self.automation_job.clean_up()
        except AttributeError:
            # sometimes the user will change the job too fast before the dosing job is created, let's protect against that.
            sleep(1)
            self.set_automation(algo_metadata)

        # reset heater back to 0.
        self._update_heater(0)

        try:
            self.logger.info(f"Starting {algo_metadata}.")
            self.automation_job = self.available_automations[algo_metadata.automation_name](
                unit=self.unit,
                experiment=self.experiment,
                temperature_control_parent=self,
                **algo_metadata.args,
            )
            self.automation = algo_metadata
            self.automation_name = algo_metadata.automation_name

            # since we are changing automations inside a controller, we know that the latest temperature reading is recent, so we can
            # pass it on to the new automation.
            # this is most useful when temp-control is initialized with only_record_temperature, and then quickly switched over to thermostat.
            self.automation_job._set_latest_temperature(self.temperature)

        except KeyError:
            self.logger.debug(
                f"Unable to find automation {algo_metadata.automation_name}. Available automations are {list(self.available_automations.keys())}. Note: You need to restart this job to have access to newly-added automations.",
                exc_info=True,
            )
            self.logger.warning(
                f"Unable to find automation {algo_metadata.automation_name}. Available automations are {list(self.available_automations.keys())}. Note: You need to restart this job to have access to newly-added automations."
            )
        except Exception as e:
            self.logger.debug(f"Change failed because of {str(e)}", exc_info=True)
            self.logger.warning(f"Change failed because of {str(e)}")

    def _update_heater(self, new_duty_cycle: float) -> bool:
        self.heater_duty_cycle = clamp(0.0, float(new_duty_cycle), 100.0)
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
                f"Temperature of heating surface has exceeded {self.MAX_TEMP_TO_DISABLE_HEATING}℃ - currently {temp}℃. This is beyond our recommendations. The heating PWM channel will be forced to 0 and the automation turned to only_record_temperature. Take caution when touching the heating surface and wetware."
            )

            self._update_heater(0)

            if self.automation_name != "only_record_temperature":
                self.set_automation(
                    TemperatureAutomation(automation_name="only_record_temperature")
                )

        elif temp > self.MAX_TEMP_TO_REDUCE_HEATING:
            self.logger.debug(
                f"Temperature of heating surface has exceeded {self.MAX_TEMP_TO_REDUCE_HEATING}℃ - currently {temp}℃. This is close to our maximum recommended value. The heating PWM channel will be reduced to 90% its current value. Take caution when touching the heating surface and wetware."
            )

            self._update_heater(self.heater_duty_cycle * 0.9)

        return temp

    def on_sleeping(self) -> None:
        self.automation_job.set_state(self.SLEEPING)

    def on_sleeping_to_ready(self) -> None:
        self.automation_job.set_state(self.READY)

    def on_disconnected(self) -> None:
        with suppress(AttributeError):
            self.publish_temperature_timer.cancel()

        with suppress(AttributeError):
            self._update_heater(0)
            self.pwm.cleanup()

        with suppress(AttributeError):
            self.automation_job.clean_up()

    def setup_heating_pcb_tmp_sensor(self):
        if whoami.is_testing_env():
            from pioreactor.utils.mock import MockTMP1075 as TMP1075
        else:
            from TMP1075 import TMP1075  # type: ignore

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

    def setup_pwm(self) -> PWM:
        hertz = 8  # technically this doesn't need to be high: it could even be 1hz. However, we want to smooth it's
        # impact (mainly: current sink), over the second. Ex: imagine freq=1hz, dc=40%, and the pump needs to run for
        # 0.3s. The influence of when the heat is one on the pump can be significant in a power-constrained system.
        pin = hardware.PWM_TO_PIN[hardware.HEATER_PWM_TO_PIN]
        pwm = PWM(pin, hertz, unit=self.unit, experiment=self.experiment)
        pwm.start(0)
        return pwm

    def infer_culture_temperature(self) -> None:
        samples = 12
        running_sum = 0.0
        for _ in range(samples):
            _temperature = self.temperature_probe_driver.temperature
            if _temperature < -242.0:
                raise ValueError("Record null temperature. Is the Pt1000 probe connected?")

            running_sum += _temperature
            sleep(0.05)

        temperature = running_sum / samples

        try:
            self.temperature = Temperature(
                temperature=round(temperature, 4),
                timestamp=current_utc_datetime(),
            )

        except Exception as e:
            self.logger.debug(e, exc_info=True)
            self.logger.error(e)


def start_temperature_control(
    automation_name: str,
    unit: Optional[str] = None,
    experiment: Optional[str] = None,
    **kwargs,
) -> TemperatureControllerWithProbe:
    return TemperatureControllerWithProbe(
        automation_name=automation_name,
        unit=unit or whoami.get_unit_name(),
        experiment=experiment or whoami.get_latest_experiment_name(),
        **kwargs,
    )


@click.command(
    name="temperature_control",
    context_settings=dict(ignore_unknown_options=True, allow_extra_args=True),
)
@click.option(
    "--automation-name",
    help="set the automation of the system",
    show_default=True,
    required=True,
)
@click.pass_context
def click_temperature_control(ctx, automation_name: str) -> None:
    """
    Start a temperature automation.
    """
    import os

    os.nice(1)

    kwargs = {
        ctx.args[i][2:].replace("-", "_"): ctx.args[i + 1] for i in range(0, len(ctx.args), 2)
    }
    if "skip_first_run" in kwargs:
        del kwargs["skip_first_run"]

    tc = start_temperature_control(
        automation_name=automation_name,
        **kwargs,
    )
    tc.block_until_disconnected()
