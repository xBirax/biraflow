"""PyInstaller entry point (the package uses relative imports, so it can't be the script)."""
import sys

from biraflow.app import main

sys.exit(main())
