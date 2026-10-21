"""Every module must import: a syntax error in a rarely-run file (the tray) once slipped
into a build because only the code paths under test were ever loaded."""
import importlib
import pkgutil

import biraflow


def test_every_module_imports():
    for info in pkgutil.walk_packages(biraflow.__path__, "biraflow."):
        if info.name != "biraflow.__main__":  # that one runs the app
            importlib.import_module(info.name)
