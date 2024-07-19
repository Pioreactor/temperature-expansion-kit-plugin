# -*- coding: utf-8 -*-
from __future__ import annotations

from setuptools import find_packages
from setuptools import setup


setup(
    name="temperature-expansion-kit-plugin",
    version="0.3.0",
    license="MIT",
    description="This plugin is necessary to use the temperature expansion kit for the Pioreactor",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author_email="hello@pioreactor.com",
    author="Pioreactor",
    url="https://github.com/Pioreactor/temperature-expansion-kit-plugin",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[],
    entry_points={
        "pioreactor.plugins": "temperature_expansion_kit_plugin = temperature_expansion_kit_plugin"
    },
)
