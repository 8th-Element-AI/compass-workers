"""
Entrypoint for `python -m deidentifier`.

Examples
--------
Default engine (regex + spaCy):
    python -m deidentifier notes.txt
    python -m deidentifier notes.txt --fast
    python -m deidentifier notes.txt --strategy mask --output clean.txt

Presidio engine:
    python -m deidentifier notes.txt --engine presidio
    python -m deidentifier notes.txt --engine presidio --format json --audit audit.json
"""
import sys

from .cli import run

sys.exit(run())
