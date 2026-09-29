"""Mark the bundled Windows x64 DLL as platform-specific in wheel metadata."""

from setuptools import setup
from setuptools.dist import Distribution


class WindowsBinaryDistribution(Distribution):
    def has_ext_modules(self):
        # The pinned attachment bridge is packaged as data, but still makes
        # this distribution Windows x64 and CPython 3.12 specific.
        return True


setup(distclass=WindowsBinaryDistribution)
