"""Setu Reflex views — the presentation layer.

``shell.py`` is the shared app chrome; ``login.py`` is the F1 sign-in gate;
``pages.py`` holds the per-route pages (built module by module).
"""

from setu.views import pages, shell

__all__ = ["pages", "shell"]