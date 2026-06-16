"""
obs-sdk plugin system.

Built-in plugins are auto-registered on import.
"""

from obs_sdk.plugins.base import ObsPlugin, PluginRegistry, obs_plugin

__all__ = ["ObsPlugin", "PluginRegistry", "obs_plugin"]

# Auto-register built-in plugins by importing them
from obs_sdk.plugins import tracing as _tracing  # noqa: F401
from obs_sdk.plugins import logging as _logging  # noqa: F401
from obs_sdk.plugins import metrics as _metrics  # noqa: F401
from obs_sdk.plugins import llm as _llm  # noqa: F401
