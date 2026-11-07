"""Optional Claude integration: a file summary for the underwriter and draft condition letters.

The model only writes prose around a decision the deterministic engine already made. It gets the
decision as read-only facts, its output is labeled advisory, and nothing it returns is parsed
back into the decision. The default provider is an offline template so the demo needs no key.
"""

from __future__ import annotations

import json
import os

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM = (
    "You help a mortgage underwriter at Kestrel Ridge Lending. A deterministic rules engine has already "
    "made the credit decision below. That decision is final. Do not change it, question it, or suggest a "
    "different outcome. Write in plain English with short sentences and concrete numbers from the facts. "
    "Do not mention any personal characteristic that is not in the facts."
)


def decision_facts(decision: dict) -> dict:
    """The only data the model sees: engine output, never raw monitoring data."""
    ws = decision.get("income_worksheet") or {}
    return {
        "application_id": decision["application_id"],
        "program": decision["program"],
        "outcome": decision["outcome"],
        "rules_version": decision["rules_version"],
        "metrics": decision["metrics"],
        "note_rate": decision["pricing"].get("note_rate"),
        "adverse_action_reasons": [r["text"] for r in decision["adverse_action_reasons"]],
        "refer_reasons": decision["refer_reasons"],
        "conditions": [c["text"] for c in decision["conditions"]],
        "income": {
            k: ws.get(k)
            for k in (
                "qualifying_monthly_income",
                "months",
                "expense_factor",
                "large_deposit_threshold",
                "nsf_count_12m",
            )
        }
        if ws
        else None,
        "dscr": (decision.get("dscr_worksheet") or {}).get("dscr"),
    }


class MockProvider:
    name = "mock"
    model = "template-v1"

    def summarize(self, facts: dict) -> dict:
        m = facts["metrics"]
        parts = [
            f"{facts['application_id']} is a {facts['program'].replace('_', ' ')} file. "
            f"The engine returned {facts['outcome'].replace('_', ' ')} under rules {facts['rules_version']}."
        ]
        if facts["income"]:
            parts.append(
                f"Qualifying income is ${facts['income']['qualifying_monthly_income']:,.2f} a month from "
                f"{facts['income']['months']} months of statements, DTI {m.get('dti', 0):.1%}."
            )
        if facts["dscr"] is not None:
            parts.append(f"DSCR is {facts['dscr']:.2f}.")
        parts.append(
            f"LTV is {m['ltv']:.1%} against a {m['max_ltv']:.1%} maximum, with "
            f"{m['reserves_months']:.1f} months of reserves."
        )
        if facts["adverse_action_reasons"]:
            parts.append("Principal reasons: " + " ".join(facts["adverse_action_reasons"]))
        if facts["refer_reasons"]:
            parts.append("Manual review because: " + " ".join(facts["refer_reasons"]))
        if facts["conditions"]:
            parts.append(f"{len(facts['conditions'])} conditions are open.")
        return {"text": " ".join(parts), "input_tokens": 0, "output_tokens": 0}

    def condition_letters(self, facts: dict) -> dict:
        letters = []
        for c in facts["conditions"]:
            letters.append(
                "Hello,\n\nTo keep your application moving, please send us the following:\n\n"
                f"- {c}\n\nYou can upload it in the borrower portal. Reply to this message with any questions.\n\n"
                "Kestrel Ridge Lending, Underwriting"
            )
        return {"letters": letters, "input_tokens": 0, "output_tokens": 0}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client=None, model: str = DEFAULT_MODEL):
        if client is None:
            import anthropic  # lazy: only needed with the claude extra

            client = anthropic.Anthropic()
        self.client = client
        self.model = model

    def _ask(self, instruction: str, facts: dict, max_tokens: int) -> tuple[str, int, int]:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"{instruction}\n\nFacts (JSON):\n{json.dumps(facts, default=str)}"}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    def summarize(self, facts: dict) -> dict:
        text, i, o = self._ask("Summarize this loan file for the underwriter in under 120 words.", facts, 400)
        return {"text": text, "input_tokens": i, "output_tokens": o}

    def condition_letters(self, facts: dict) -> dict:
        if not facts["conditions"]:
            return {"letters": [], "input_tokens": 0, "output_tokens": 0}
        text, i, o = self._ask(
            "Draft one short, polite letter to the borrower for each condition, asking for the document. "
            "Separate letters with a line containing only ---.",
            facts,
            1200,
        )
        return {
            "letters": [p.strip() for p in text.split("\n---\n") if p.strip()],
            "input_tokens": i,
            "output_tokens": o,
        }


def get_provider(name: str | None = None, client=None):
    name = name or os.environ.get("UW_LLM_PROVIDER", "mock")
    if name == "anthropic":
        return AnthropicProvider(client=client, model=os.environ.get("UW_LLM_MODEL", DEFAULT_MODEL))
    return MockProvider()


def assist(provider, decision: dict) -> dict:
    facts = decision_facts(decision)
    s = provider.summarize(facts)
    letters = provider.condition_letters(facts)
    return {
        "advisory": True,
        "note": "Drafted by a language model from the engine's output. The engine decision is the source of truth.",
        "provider": provider.name,
        "model": provider.model,
        "decision_outcome": decision["outcome"],
        "summary": s["text"],
        "condition_letters": letters["letters"],
        "usage": {
            "input_tokens": s["input_tokens"] + letters["input_tokens"],
            "output_tokens": s["output_tokens"] + letters["output_tokens"],
        },
    }
