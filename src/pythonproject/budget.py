"""Spend ledger and hard cap for live TypeSafe calls made by experiment and recording scripts.

    python -m pythonproject.budget --report

Every guarded request is appended to LEDGER as one JSON line. Before a request is sent, the
ledger total plus everything in flight plus this request's estimate must stay within the cap
($2.00 by default; override with TYPESAFE_BUDGET_USD), otherwise BudgetExceeded is raised and
nothing is sent.
"""

import json
import os
import sys
import time
from pathlib import Path

from .questions import PRICE_PER_TOKEN  # noqa: E402  (re-exported for scripts)
TOKENS_PER_IMAGE = 4_200              # measured ~4,050-4,100 per prepared 1568x882 photo, plus margin
LEDGER = Path(__file__).resolve().parents[2] / ".typesafe_spend.jsonl"


class BudgetExceeded(RuntimeError):
    pass


def cap_usd() -> float:
    return float(os.environ.get("TYPESAFE_BUDGET_USD", "2.00"))


def spent_usd(ledger: Path = LEDGER) -> float:
    if not ledger.exists():
        return 0.0
    return sum(json.loads(line)["usd"] for line in ledger.read_text().splitlines() if line.strip())


class GuardedClient:
    """Wraps an AsyncTypeSafeClient; refuses requests that could exceed the cap, and logs usage."""

    def __init__(self, client, script: str, ledger: Path = LEDGER, cap: float | None = None):
        self.client = client
        self.script = script
        self.ledger = ledger
        self.cap = cap_usd() if cap is None else cap
        self._spent = spent_usd(ledger)
        self._in_flight = 0.0

    async def system_one(self, *args, arm: str = "", photo: str = "", images: int = 1, **kwargs):
        estimate = images * TOKENS_PER_IMAGE * PRICE_PER_TOKEN
        if self._spent + self._in_flight + estimate > self.cap:
            raise BudgetExceeded(
                f"budget cap ${self.cap:.2f} reached: spent ${self._spent:.4f}, "
                f"in flight ${self._in_flight:.4f}, next request ~${estimate:.4f}")
        self._in_flight += estimate
        try:
            response = await self.client.system_one(*args, **kwargs)
        finally:
            self._in_flight -= estimate
        tokens = response.usage.input_tokens or 0
        usd = tokens * PRICE_PER_TOKEN
        self._spent += usd
        with open(self.ledger, "a") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": self.script,
                                "arm": arm, "photo": photo, "input_tokens": tokens,
                                "usd": usd}) + "\n")
        return response

    @property
    def spent(self) -> float:
        return self._spent


def report(ledger: Path = LEDGER) -> str:
    if not ledger.exists():
        return f"no spend recorded (cap ${cap_usd():.2f})"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    by: dict[str, list[float]] = {}
    for r in rows:
        k = f"{r['script']}:{r['arm']}" if r["arm"] else r["script"]
        by.setdefault(k, [0, 0.0, 0])
        by[k][0] += 1
        by[k][1] += r["usd"]
        by[k][2] += r["input_tokens"]
    lines = [f"{'script:arm':<44} {'requests':>8} {'tokens':>11} {'usd':>8}"]
    for k, (n, usd, tok) in sorted(by.items()):
        lines.append(f"{k:<44} {n:>8} {tok:>11,} {usd:>8.4f}")
    total = sum(r["usd"] for r in rows)
    lines.append(f"{'TOTAL':<44} {len(rows):>8} {sum(r['input_tokens'] for r in rows):>11,} "
                 f"{total:>8.4f}   (cap ${cap_usd():.2f}, left ${cap_usd() - total:.4f})")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--report" in sys.argv:
        print(report())
