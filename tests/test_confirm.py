import httpx
import pytest

from capital_hook import APIError, Confirmation


async def test_confirm_returns_the_accepted_confirmation_with_the_deal_id(client, fake):
    reference = await client.open_position("GOLD", 1.5, "BUY")
    confirmation = await client.confirm(reference)
    (deal_id,) = fake.positions
    assert isinstance(confirmation, Confirmation)
    assert confirmation.accepted and not confirmation.rejected
    assert (confirmation.deal_reference, confirmation.epic, confirmation.direction) == (reference, "GOLD", "BUY")
    assert confirmation.position_deal_id == deal_id and confirmation.deal_id and confirmation.deal_id != deal_id     # the order is not the position
    assert (confirmation.size, confirmation.level, confirmation.status, confirmation.reason) == (1.5, 2001.0, "OPEN", "SUCCESS")
    assert confirmation.affected_deals == ({"dealId": deal_id, "status": "OPENED"},)
    assert confirmation.raw["dealStatus"] == "ACCEPTED"


async def test_confirm_reports_a_rejection_without_a_deal(client, fake):
    fake.confirmations["o_rejected"] = {"dealReference": "o_rejected", "dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS", "status": "DELETED", "epic": "GOLD", "affectedDeals": []}
    confirmation = await client.confirm("o_rejected")
    assert confirmation.rejected and not confirmation.accepted
    assert confirmation.reason == "INSUFFICIENT_FUNDS" and confirmation.deal_id is None and confirmation.affected_deals == ()
    assert confirmation.size is None and confirmation.level is None


async def test_confirm_retries_while_the_confirmation_is_not_available_yet(client, fake):
    reference = await client.open_position("GOLD", 1, "BUY")
    fake.confirm_delay = 2
    confirmation = await client.confirm(reference, attempts=5, delay=0)
    assert confirmation.accepted
    assert sum(1 for method, path, _ in fake.requests if path.startswith("/api/v1/confirms/")) == 3


async def test_confirm_gives_up_with_the_404_after_the_attempts(client, fake):
    with pytest.raises(APIError) as err:
        await client.confirm("o_unknown", attempts=3, delay=0)
    assert err.value.status_code == 404 and err.value.error_code == "error.not-found.dealReference"
    assert sum(1 for _, path, _ in fake.requests if path.startswith("/api/v1/confirms/")) == 3


async def test_confirm_does_not_retry_errors_other_than_404(make_client):
    seen = []

    def server_error(request):
        if request.url.path.startswith("/api/v1/confirms/"):
            seen.append(request.url.path)
            return httpx.Response(500, json={"errorCode": "error.server"})

    client = make_client(server_error)
    with pytest.raises(APIError) as err:
        await client.confirm("o_x", attempts=5, delay=0)
    assert err.value.status_code == 500 and len(seen) == 1


def test_confirmation_from_response_tolerates_missing_fields():
    confirmation = Confirmation.from_response({})
    assert confirmation.deal_reference == "" and confirmation.deal_status == "" and confirmation.deal_id is None
    assert not confirmation.accepted and not confirmation.rejected and confirmation.affected_deals == ()


def test_confirmation_is_immutable():
    confirmation = Confirmation.from_response({"dealReference": "o_1", "dealStatus": "ACCEPTED"})
    with pytest.raises(Exception):
        confirmation.deal_status = "REJECTED"
