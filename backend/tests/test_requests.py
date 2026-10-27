"""Staff replies to participant requests (needs TT_PG_BIN)."""

from conftest import OTHER_SITE_CLAIMS
from test_api_integration import enroll


def ask(call_participant, pid, kind="reminder_change", message="Could my evening reminder be at 9 PM?"):
    status, out = call_participant(pid, "POST", "/me/requests", {"kind": kind, "message": message})
    assert status == 201, out
    return out["request"]["comm_id"]


def test_quick_and_long_replies_reach_the_participant(call_staff, call_participant):
    pid = enroll(call_staff)
    cid = ask(call_participant, pid)

    queue = call_staff("GET", "/staff/requests")[1]
    assert queue["unanswered"] == 1 and queue["requests"][0]["answered"] is False

    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "Your reminder time has been updated."})[0] == 201
    long = "Hi Maria,\n\n" + "We moved your evening dose reminder to 9 PM. " * 20
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": long})[0] == 201

    queue = call_staff("GET", "/staff/requests")[1]
    assert queue["unanswered"] == 0 and len(queue["requests"][0]["replies"]) == 2
    assert queue["requests"][0]["replies"][0]["from_addr"] == "coordinator@riverside.example.org"

    mine = call_participant(pid, "GET", "/me/requests")[1]["requests"][0]
    assert [r["body_text"] for r in mine["replies"]][0] == "Your reminder time has been updated."
    assert "from_addr" not in mine["replies"][0]
    # Replies are not chat messages.
    assert call_participant(pid, "GET", "/me/chat")[1]["messages"] == []


def test_unanswered_requests_sort_first(call_staff, call_participant):
    pid = enroll(call_staff)
    first, second = ask(call_participant, pid, message="one"), ask(call_participant, pid, message="two")
    call_staff("POST", f"/staff/requests/{second}/reply", {"text": "ok"})
    order = [(r["body_text"], r["answered"]) for r in call_staff("GET", "/staff/requests")[1]["requests"]]
    assert order == [("one", False), ("two", True)]


def test_validation_and_site_scope(call_staff, call_participant):
    pid = enroll(call_staff)
    cid = ask(call_participant, pid)
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "  "})[0] == 400
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "x" * 4001})[0] == 400
    assert call_staff("POST", "/staff/requests/not-a-uuid/reply", {"text": "hi"})[0] == 400
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "hi"}, claims=OTHER_SITE_CLAIMS)[0] == 404
    # Only requests can be replied to, not chat messages.
    call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    comms = call_staff("GET", f"/staff/participants/{pid}/communications")[1]["communications"]
    chat_id = next(c["comm_id"] for c in comms if c["msg_type"] == "text")
    assert call_staff("POST", f"/staff/requests/{chat_id}/reply", {"text": "hi"})[0] == 404


def test_other_participants_do_not_see_replies(call_staff, call_participant):
    a = enroll(call_staff)
    b = enroll(call_staff, phone_e164="+17325550143", full_name="Bo Park")
    cid = ask(call_participant, a)
    call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "private"})
    assert call_participant(b, "GET", "/me/requests")[1]["requests"] == []
