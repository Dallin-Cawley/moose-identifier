"""The spend guard: refuses requests past the cap, logs every request's real usage."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from pythonproject.budget import PRICE_PER_TOKEN, BudgetExceeded, GuardedClient, spent_usd


class FakeClient:
    def __init__(self, tokens=4000):
        self.calls = 0
        self.tokens = tokens

    async def system_one(self, *a, **k):
        self.calls += 1
        await asyncio.sleep(0)  # yield, so concurrent requests really overlap
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=self.tokens))


def test_logs_usage(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    g = GuardedClient(FakeClient(), "t", ledger=ledger, cap=1.0)
    asyncio.run(g.system_one({}, {}, arm="a", photo="p.jpg"))
    [row] = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert (row["arm"], row["photo"], row["input_tokens"]) == ("a", "p.jpg", 4000)
    assert spent_usd(ledger) == pytest.approx(4000 * PRICE_PER_TOKEN)


def test_refuses_past_cap_without_sending(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"usd": 0.9999, "input_tokens": 0, "script": "x", "arm": "", "photo": ""}) + "\n")
    fake = FakeClient()
    g = GuardedClient(fake, "t", ledger=ledger, cap=1.0)
    with pytest.raises(BudgetExceeded):
        asyncio.run(g.system_one({}, {}))
    assert fake.calls == 0


def test_in_flight_requests_count_toward_cap(tmp_path):
    # cap allows ~1 image request; two concurrent requests must not both go out.
    # tokens=0 so the first request's *logged* spend can't be what stops the second.
    fake = FakeClient(tokens=0)
    g = GuardedClient(fake, "t", ledger=tmp_path / "l.jsonl", cap=4200 * PRICE_PER_TOKEN * 1.5)

    async def both():
        return await asyncio.gather(g.system_one({}, {}), g.system_one({}, {}), return_exceptions=True)

    results = asyncio.run(both())
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 1 and fake.calls == 1
