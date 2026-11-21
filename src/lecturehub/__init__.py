"""Lecture Hub. 정본 명세: docs/Lecture_Hub_Plan.md"""
import os

# numpy's OpenBLAS starts one thread (with its own buffer) per logical CPU on import: 22 threads and ~700 MB
# of committed memory per process here, for small vectors that never need them (resource review 2026-11-09).
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

__version__ = "1.0.1"
