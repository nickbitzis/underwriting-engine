"""Load versioned rule files and pick the one in effect on a date."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

DEFAULT_RULES_DIR = Path(__file__).resolve().parents[2] / "rules"
REQUIRED_KEYS = ("version", "effective_date", "income", "products", "pricing", "adverse_action")


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class RuleSet:
    version: str
    effective_date: date
    sha256: str
    path: str
    data: dict

    def __getitem__(self, key):
        return self.data[key]

    def product(self, program: str) -> dict:
        return self.data["products"][program]


def _validate(data: dict, path: str) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise RulesError(f"{path}: missing keys {missing}")
    bands = data["pricing"]["ltv_bands"]
    if bands != sorted(bands):
        raise RulesError(f"{path}: ltv_bands must be ascending")
    for row in data["pricing"]["llpa_grid"]:
        if len(row["adj"]) != len(bands):
            raise RulesError(
                f"{path}: llpa row fico_min={row['fico_min']} has {len(row['adj'])} cells, need {len(bands)}"
            )
    for name, prod in data["products"].items():
        mins = [r["fico_min"] for r in prod["max_ltv"]]
        if mins != sorted(mins, reverse=True):
            raise RulesError(f"{path}: {name}.max_ltv rows must be in descending fico_min order")


def load_rules(path: str | Path) -> RuleSet:
    p = Path(path)
    raw = p.read_bytes()
    data = yaml.safe_load(raw)
    _validate(data, str(p))
    eff = data["effective_date"]
    eff = eff if isinstance(eff, date) else date.fromisoformat(str(eff))
    return RuleSet(
        version=str(data["version"]),
        effective_date=eff,
        sha256=hashlib.sha256(raw).hexdigest(),
        path=p.name,
        data=data,
    )


def load_all(rules_dir: str | Path = DEFAULT_RULES_DIR) -> list[RuleSet]:
    sets = [load_rules(p) for p in sorted(Path(rules_dir).glob("*.yaml"))]
    versions = [s.version for s in sets]
    if len(set(versions)) != len(versions):
        raise RulesError(f"duplicate rule versions in {rules_dir}: {versions}")
    return sorted(sets, key=lambda s: s.effective_date)


def select_version(rule_sets: list[RuleSet], as_of: date | str) -> RuleSet:
    """The rule set in effect on `as_of`: latest effective_date that is not after it."""
    d = as_of if isinstance(as_of, date) else date.fromisoformat(as_of)
    eligible = [s for s in rule_sets if s.effective_date <= d]
    if not eligible:
        raise RulesError(f"no rules in effect on {d}")
    return max(eligible, key=lambda s: s.effective_date)
