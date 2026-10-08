"""QGIS plugin entry point."""


def classFactory(iface):
    from .flood_simulation import FloodSimulationPlugin

    return FloodSimulationPlugin(iface)
