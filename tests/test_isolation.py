"""The protected-class attribute is for monitoring only. The engine must not be able to see it."""

import io
import tokenize
from dataclasses import fields, replace
from pathlib import Path

from conftest import NOW, bs_input

from uwengine.engine import decide
from uwengine.llm import decision_facts
from uwengine.models import EngineInput, Monitoring, to_engine_input
from uwengine.synth import generate_portfolio

PKG = Path(__file__).resolve().parents[1] / "src" / "uwengine"
DECISION_PATH = ["engine.py", "income.py", "pricing.py", "rules.py"]
FORBIDDEN = ("monitoring", "protected_group", "Monitoring")


def code_names(path: Path) -> set[str]:
    """Identifiers in the file, ignoring comments and docstrings."""
    toks = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
    return {t.string for t in toks if t.type == tokenize.NAME}


def test_engine_input_has_no_monitoring_field():
    names = {f.name for f in fields(EngineInput)}
    assert not any(word in n for n in names for word in ("monitor", "protected", "group", "race", "sex", "age"))


def test_to_engine_input_drops_monitoring():
    lf = generate_portfolio(1)[0]
    assert lf.monitoring is not None
    inp = to_engine_input(lf)
    assert not hasattr(inp, "monitoring")


def test_decision_path_never_names_the_attribute():
    for name in DECISION_PATH:
        hits = code_names(PKG / name) & set(FORBIDDEN)
        assert not hits, f"{name} references {hits}"


def test_changing_the_protected_group_never_changes_a_decision(rules):
    for lf in generate_portfolio(60):
        decisions = []
        for group in ("group_a", "group_b", "group_c"):
            variant = replace(lf, monitoring=Monitoring(group))
            decisions.append(decide(to_engine_input(variant), rules, decision_id="x", now=NOW).to_dict())
        assert decisions[0] == decisions[1] == decisions[2]


def test_generator_draws_group_independently_of_inputs():
    """Mean credit score should not differ much by group in the synthetic data."""
    files = generate_portfolio(300)
    by = {}
    for f in files:
        by.setdefault(f.monitoring.protected_group, []).append(f.borrower.credit_score)
    means = [sum(v) / len(v) for v in by.values()]
    assert len(by) == 3
    assert max(means) - min(means) < 15


def test_model_facts_carry_no_monitoring(rules):
    d = decide(bs_input(), rules, now=NOW).to_dict()
    facts = decision_facts(d)
    blob = str(facts).lower()
    assert "group" not in blob and "monitoring" not in blob
