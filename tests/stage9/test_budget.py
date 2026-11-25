from pathlib import Path

import pytest

from benchmarks.budget import BudgetExceededError, BudgetLedger, BudgetPolicy


def test_budget_reserves_attempts_before_dispatch_and_reconciles(
    tmp_path: Path,
) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", BudgetPolicy(2, 10, 100))
    assert ledger.reserve_attempt() == 1
    ledger.reconcile(input_tokens=2, output_tokens=1, cost_micro_usd=3)
    assert ledger.reserve_attempt() == 2
    with pytest.raises(BudgetExceededError):
        ledger.reserve_attempt()
    with pytest.raises(BudgetExceededError):
        ledger.reconcile(input_tokens=99, output_tokens=0, cost_micro_usd=0)


def test_budget_corruption_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text("not-json")
    with pytest.raises(BudgetExceededError):
        BudgetLedger(path, BudgetPolicy(1, 1, 1)).reserve_attempt()


@pytest.mark.asyncio
async def test_fake_transport_is_counted_before_dispatch(tmp_path: Path) -> None:
    from benchmarks.budget import BudgetedTransport

    calls = 0

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    ledger = BudgetLedger(tmp_path / "b.json", BudgetPolicy(1, 10, 10))
    transport = BudgetedTransport(httpx.MockTransport(handler), ledger)
    async with httpx.AsyncClient(transport=transport) as client:  # type: ignore[arg-type]
        assert (await client.get("https://example.invalid")).status_code == 200
        with pytest.raises(BudgetExceededError):
            await client.get("https://example.invalid")
    assert calls == 1
