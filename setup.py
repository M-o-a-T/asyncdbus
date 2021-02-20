#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import os

from setuptools import setup

# Package meta-data.
DESCRIPTION = 'A zero-dependency async DBus library'
REQUIRES_PYTHON = '>=3.7.0'

# What packages are required for this module to be executed?
REQUIRED = []

# What packages are optional?
EXTRAS = {}

# The rest you shouldn't have to touch too much :)
# ------------------------------------------------
# Except, perhaps the License and Trove Classifiers!
# If you do change the License, remember to change the Trove Classifier for that!

here = os.path.abspath(os.path.dirname(__file__))

__title__ = 'dbus_next'
__description__ = 'A zero-dependency async DBus library'
__url__ = 'https://github.com/M-o-a-T/asyncdbus'
__author__ = 'Matthias Urlichs'
__author_email__ = 'matthias@urlichs.de'
__license__ = 'MIT'
__copyright__ = 'Copyright 2019 Tony Crisci, 2021 Matthias Urlichs'

with io.open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = '\n' + f.read()

setup(
    name=__title__,
    use_scm_version={"version_scheme": "guess-next-dev", "local_scheme": "dirty-tag"},
    setup_requires=["setuptools_scm"],
    description=DESCRIPTION,
    long_description=long_description,
    long_description_content_type='text/markdown',
    author=__author__,
    author_email=__author_email__,
    python_requires=REQUIRES_PYTHON,
    url=__url__,
    packages=["asyncdbus"],
    install_requires=REQUIRED,
    extras_require=EXTRAS,
    include_package_data=True,
    license='MIT',
    classifiers=[
        # Full list: https://pypi.python.org/pypi?%3Aaction=list_classifiers
        'Development Status :: 3 - Alpha',
        'Environment :: X11 Applications',
        'Environment :: X11 Applications :: Gnome',
        'Topic :: Desktop Environment :: Gnome',
        'Topic :: Software Development :: Embedded Systems',
        'Framework :: AsyncIO',
        'Framework :: Trio',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy',
        'Typing :: Typed'
    ])
