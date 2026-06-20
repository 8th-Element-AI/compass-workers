"""
Entrypoint for `python -m deidentifier`.

Examples
--------
    python -m deidentifier notes.txt
    python -m deidentifier notes.txt --format json
    python -m deidentifier notes.txt --ner-model en_core_web_lg
"""
import sys

from .cli import run

sys.exit(run())
