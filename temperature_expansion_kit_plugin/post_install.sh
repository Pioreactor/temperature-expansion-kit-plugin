#!/bin/bash

set -x
set -e

export LC_ALL=C

crudini --set .pioreactor/config.ini temperature_automation.thermostat Kp 0.014
crudini --set .pioreactor/config.ini temperature_automation.thermostat Ki 0.0
crudini --set .pioreactor/config.ini temperature_automation.thermostat Kd 3.5
