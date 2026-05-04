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


class MerchantEntry:
    __slots__ = ("categories", "same_day_posting")

    def __init__(self, categories: list[str], same_day_posting: bool = False) -> None:
        self.categories = categories
        self.same_day_posting = same_day_posting


def load_merchants(path: Path) -> dict[str, MerchantEntry]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping of merchant -> categories")
    out: dict[str, MerchantEntry] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            if not all(isinstance(x, str) for x in v):
                raise ValueError(f"{path}: merchant {k!r} must map to list[str]")
            out[k] = MerchantEntry(categories=v)
        elif isinstance(v, dict):
            cats = v.get("categories", [])
            if not isinstance(cats, list) or not all(isinstance(x, str) for x in cats):
                raise ValueError(f"{path}: merchant {k!r} 'categories' must be list[str]")
            out[k] = MerchantEntry(
                categories=cats,
                same_day_posting=bool(v.get("same_day_posting", False)),
            )
        else:
            raise ValueError(f"{path}: merchant {k!r} must map to list[str] or dict")
    return out
