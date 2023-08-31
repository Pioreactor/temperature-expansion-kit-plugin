# -*- coding: utf-8 -*-
from __future__ import annotations

from setuptools import find_packages
from setuptools import setup


setup(
    name="temperature-expansion-kit-plugin",
    version="0.0.1",
    license="MIT",
    description="",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author_email="cam@pioreactor.com",
    author="Cam Davidson-Pilon",
    url="",
    packages=find_packages(),
    include_package_data=True,
    install_requires=["adafruit-circuitpython-max31865"],
    entry_points={"pioreactor.plugins": "temperature_expansion_kit_plugin = temperature_expansion_kit_plugin"},
)