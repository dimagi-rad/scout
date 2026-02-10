"""
Connector registry for looking up connectors by source type.
"""
from typing import TYPE_CHECKING

from apps.datasources.models import DataSourceType

from .base import BaseConnector

if TYPE_CHECKING:
    from apps.datasources.models import DataSource

# Registry of connector classes by source type
_CONNECTORS: dict[str, type[BaseConnector]] = {}


def register_connector(source_type: str):
    """
    Decorator to register a connector class for a source type.

    Usage:
        @register_connector(DataSourceType.COMMCARE)
        class CommCareConnector(BaseConnector):
            ...
    """

    def decorator(cls: type[BaseConnector]) -> type[BaseConnector]:
        _CONNECTORS[source_type] = cls
        return cls

    return decorator


def get_connector(data_source: "DataSource") -> BaseConnector:
    """
    Get a connector instance for the given data source.

    Args:
        data_source: The DataSource model instance

    Returns:
        Instantiated connector for the source type

    Raises:
        ValueError: If no connector is registered for the source type
    """
    connector_class = _CONNECTORS.get(data_source.source_type)
    if connector_class is None:
        raise ValueError(f"No connector registered for source type: {data_source.source_type}")
    return connector_class(data_source)


def get_available_source_types() -> list[str]:
    """Return list of source types that have registered connectors."""
    return list(_CONNECTORS.keys())


# Import connectors to trigger registration
# This must be at the bottom to avoid circular imports
def _register_all_connectors():
    """Import all connector modules to register them."""
    from . import commcare  # noqa: F401

    # Future connectors:
    # from . import commcare_connect  # noqa: F401


_register_all_connectors()
