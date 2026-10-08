"""Serialise a :class:`Report` into the JSON document required by the brief, and write it."""

import json
import os
import shutil
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from ecommerce_scraper.models import Report, ResultItem


def render_json(report: Report, *, indent: int | None = 2) -> str:
    document = {
        "results": [_item(item) for item in report.results],
        "total": _number(report.total),
    }
    return json.dumps(document, indent=indent) + "\n"


def _item(item: ResultItem) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "name": item.name,
        "description": item.description,
        "price": _number(item.price),
    }
    if item.colors:  # optional in the contract: omitted, never null
        fields["colors"] = list(item.colors)
    return fields


def _number(value: Decimal) -> float:
    # The contract wants JSON numbers, and the stdlib encoder only emits those for
    # int/float. Converting at the very edge is lossless here: Python's float repr
    # is the shortest string that round-trips, so a cents amount with fewer than 16
    # significant digits is written exactly as the Decimal ("1178.19"). Always a
    # float, so consumers see one type even for whole-dollar prices ("1149.0").
    number = float(value)
    if Decimal(repr(number)) != value:  # never let precision go missing unnoticed
        raise ValueError(f"{value} cannot be written exactly as a JSON number")
    return number


def write_report(text: str, destination: str) -> None:
    """Write to stdout (``"-"``), through a device such as ``/dev/null``, or atomically
    to a file: a temporary file is renamed over the destination, so readers never see
    a half-written report and a failed write never destroys the previous one."""
    if destination == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    path = Path(destination)
    if path.exists() and not path.is_file():  # a device or pipe can't be replaced
        with path.open("w", encoding="utf-8") as stream:
            stream.write(text)
        return
    # Created normally, so it honours the umask (mkstemp would force 0600), and it
    # inherits the permissions of any report it replaces.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())  # on disk before the rename makes it the report
        if path.is_file():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
