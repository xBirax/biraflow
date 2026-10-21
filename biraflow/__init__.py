"""Bira Flow: system-wide push-to-talk dictation for Windows."""
import os

# numpy's OpenBLAS starts a thread per core and commits ~610 MB for them, and the app does no
# linear algebra (measured: 628 -> 17 MB private, 25 -> 6 threads). It must be set before numpy
# is first imported; whisper.cpp's BLAS build sets its own thread count from -t.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

__version__ = "1.1.1"
