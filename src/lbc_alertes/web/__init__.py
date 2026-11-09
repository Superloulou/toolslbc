"""Interface web : collecte, annonces et tamis IA."""

from .app import Collector, RingBuffer, serve

__all__ = ["Collector", "RingBuffer", "serve"]
