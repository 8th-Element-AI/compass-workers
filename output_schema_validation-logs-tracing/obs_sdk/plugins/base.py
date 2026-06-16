"""
Plugin base class and registry for obs-sdk.

Every obs-sdk component (tracing, logging, metrics, LLM) is a plugin that
implements the ObsPlugin interface.  The PluginRegistry stores plugin
classes by name; the ObservabilityClient loads and initialises them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

from obs_sdk.config import ObsConfig


class ObsPlugin(ABC):
    """Base class for all obs-sdk plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin name (e.g. ``'tracing'``, ``'logging'``)."""
        ...

    @abstractmethod
    def initialize(self, config: ObsConfig, **dependencies: Any) -> None:
        """
        Called by the client after construction.

        Parameters
        ----------
        config:
            Fully resolved :class:`~obs_sdk.config.ObsConfig`.
        **dependencies:
            Already-initialised plugins keyed by name so that plugins
            can reference each other (e.g. the LLM plugin needs tracing
            and logging).
        """
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Gracefully flush and shut down this plugin."""
        ...


class PluginRegistry:
    """
    Class-level registry that maps plugin names to plugin classes.

    Built-in plugins register themselves via the ``@obs_plugin`` decorator.
    """

    _plugin_classes: Dict[str, Type[ObsPlugin]] = {}

    @classmethod
    def register(cls, name: str, plugin_class: Type[ObsPlugin]) -> None:
        """Register a plugin class under *name*."""
        cls._plugin_classes[name] = plugin_class

    @classmethod
    def get_class(cls, name: str) -> Optional[Type[ObsPlugin]]:
        """Return the plugin class registered under *name*, or ``None``."""
        return cls._plugin_classes.get(name)

    @classmethod
    def all_registered(cls) -> Dict[str, Type[ObsPlugin]]:
        """Return a copy of all registered plugin classes."""
        return dict(cls._plugin_classes)


def obs_plugin(name: str):
    """Class decorator that auto-registers a plugin in the PluginRegistry.

    When Python imports a plugin file (e.g. tracing.py), the @obs_plugin("tracing")
    decorator runs and adds TracingPlugin to the registry.  Later, when
    ObservabilityClient.__init__() loops through _BUILTIN_PLUGIN_ORDER, it
    looks up each name in the registry to find the class to instantiate.

    Usage::

        @obs_plugin("tracing")
        class TracingPlugin(ObsPlugin):
            ...
    """
    def decorator(cls: Type[ObsPlugin]) -> Type[ObsPlugin]:
        PluginRegistry.register(name, cls)
        return cls
    return decorator
