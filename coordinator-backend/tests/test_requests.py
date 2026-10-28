"""Staff replies to participant requests (needs TT_PG_BIN)."""

from conftest import OTHER_SITE_CLAIMS
from test_api_integration import enroll


def ask(call_participant, call_staff, pid, kind="reminder_change", message="Could my evening reminder be at 9 PM?"):
    status, out = call_participant(pid, "POST", "/me/requests", {"kind": kind, "message": message})
    assert status == 201, out
    # The participant can't read the new row back under db/ grants; staff find it in their queue.
    queue = call_staff("GET", "/staff/requests")[1]["requests"]
    return next(r["comm_id"] for r in queue if r["participant_id"] == pid and r["body_text"] == message)


def test_quick_and_long_replies_reach_the_participant(call_staff, call_participant):
    pid = enroll(call_staff)
    cid = ask(call_participant, call_staff, pid)

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
    first = ask(call_participant, call_staff, pid, message="one")
    second = ask(call_participant, call_staff, pid, message="two")
    call_staff("POST", f"/staff/requests/{second}/reply", {"text": "ok"})
    order = [(r["body_text"], r["answered"]) for r in call_staff("GET", "/staff/requests")[1]["requests"]]
    assert order == [("one", False), ("two", True)]


def test_validation_and_site_scope(call_staff, call_participant):
    pid = enroll(call_staff)
    cid = ask(call_participant, call_staff, pid)
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "  "})[0] == 400
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "x" * 4001})[0] == 400
    assert call_staff("POST", "/staff/requests/not-a-uuid/reply", {"text": "hi"})[0] == 400
    assert call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "hi"}, claims=OTHER_SITE_CLAIMS)[0] == 404
    # Only requests can be replied to, not chat messages.
    call_participant(pid, "POST", "/me/chat", {"text": "hello"})
    comms = call_staff("GET", f"/staff/participants/{pid}/communications")[1]["communications"]
    chat_id = next(c["comm_id"] for c in comms if c["msg_type"] == "text")
    assert call_staff("POST", f"/staff/requests/{chat_id}/reply", {"text": "hi"})[0] == 404


def test_reply_links_to_the_request_however_the_id_is_written(call_staff, call_participant):
    pid = enroll(call_staff)
    cid = ask(call_participant, call_staff, pid)
    assert call_staff("POST", f"/staff/requests/{cid.upper()}/reply", {"text": "Done"})[0] == 201
    assert call_staff("GET", "/staff/requests")[1]["requests"][0]["answered"] is True
    assert call_participant(pid, "GET", "/me/requests")[1]["requests"][0]["replies"][0]["body_text"] == "Done"


def test_other_participants_do_not_see_replies(call_staff, call_participant):
    a = enroll(call_staff)
    b = enroll(call_staff, phone_e164="+17325550143", full_name="Bo Park")
    cid = ask(call_participant, call_staff, a)
    call_staff("POST", f"/staff/requests/{cid}/reply", {"text": "private"})
    assert call_participant(b, "GET", "/me/requests")[1]["requests"] == []


def test_attention_queue_shape_and_site_scope(call_staff):
    status, out = call_staff("GET", "/staff/attention")
    assert status == 200 and isinstance(out["items"], list)
    for i in out["items"]:
        assert {"priority", "item", "participant_id", "site_id", "detail", "note", "happened_at"} <= set(i)
    priorities = [i["priority"] for i in out["items"]]
    assert priorities == sorted(priorities)
    # Another site's staff get a list too, scoped by row-level security to their own site.
    assert call_staff("GET", "/staff/attention", claims=OTHER_SITE_CLAIMS)[0] == 200
