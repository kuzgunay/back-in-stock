"""Jinja2 environment. Autoescaping is on for every .html template."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from .util import from_iso

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _fmt_time(value: str | None) -> str:
    dt = from_iso(value)
    if dt is None:
        return ""
    return dt.strftime("%b %d, %Y %H:%M:%S UTC").replace(" 0", " ", 1)


def _relative(value: str | None, now: datetime | None = None) -> str:
    dt = from_iso(value)
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    seconds = (now - dt).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 45:
        text = "just now" if not future else "in a few seconds"
        return text
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            label = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {label}" if future else f"{label} ago"
    return "just now"


env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["fmt_time"] = _fmt_time
env.filters["relative"] = _relative


def render(template_name: str, /, **context: object) -> str:
    return env.get_template(template_name).render(**context)
