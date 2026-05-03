from pathlib import Path

import yaml

from .models import Card


def load_cards(path: Path) -> dict[str, Card]:
    raw = yaml.safe_load(path.read_text())
    cards = [Card.model_validate(c) for c in raw]
    by_id: dict[str, Card] = {}
    for c in cards:
        if c.id in by_id:
            raise ValueError(f"duplicate card id in {path}: {c.id}")
        by_id[c.id] = c
    return by_id


def load_merchants(path: Path) -> dict[str, list[str]]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping of merchant -> [categories]")
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{path}: merchant {k!r} must map to list[str]")
        out[k] = v
    return out
