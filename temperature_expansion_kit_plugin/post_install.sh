#!/bin/bash

set -x
set -e

export LC_ALL=C

echo 'dtparam=spi=on' | sudo tee -a /boot/config.txt
