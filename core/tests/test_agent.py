from __future__ import annotations

from bobd.agent import Source, ToolRecord, TurnState, exposed_name, rank_tools, verify
from bobd.broker import Caller
from bobd.router import Route, Specialist, keyword_route


async def test_request_is_routed_acts_and_replies_with_sources(bob, model, fake_ollama):
    model.steps = [
        ("knowledge_search", {"collection": "notes", "query": "roadmap"}),
        "The roadmap is in the doc [1].",
    ]
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    reply = await bob.orchestrator.handle("what does my roadmap note say?", emit=emit)
    assert reply.specialist == "notes" and reply.route.routed_by == "model"
    assert reply.text == "The roadmap is in the doc [1]."
    assert [s.citation for s in reply.sources] == ["notes:doc.md:3@abc123"]
    assert reply.warnings == [] and reply.steps == 2
    assert [(t.plugin, t.tool, t.outcome) for t in reply.tools] == [("knowledge", "search", "ok")]

    kinds = [event["type"] for event in events]
    assert kinds[0] == "route" and kinds[-1] == "final"
    assert "tool_call" in kinds and "tool_result" in kinds and "token" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "token") == reply.text

    # The specialist saw only model tools: no admin tool, and at most the cap.
    offered = model.seen_tools[0]
    assert "notes_wipe" not in offered and "notes_add" in offered and "knowledge_search" in offered
    assert len(offered) <= bob.config.agent.max_tools_per_turn
    assert "You keep the user's notes." in model.seen_instructions[0]
    assert model.roles == ["main"]
    # The router ran on the router model with a schema that only allows real domains.
    router_call = fake_ollama.chat_requests[0]
    assert router_call["model"] == "router-model"
    assert router_call["format"]["properties"]["domain"]["enum"] == ["notes", "general"]


async def test_conversation_memory_carries_over(bob, model):
    first = await bob.orchestrator.handle("remember the plan")
    model.steps = ["second answer"]
    second = await bob.orchestrator.handle("and then?", conversation_id=first.conversation_id)
    assert second.conversation_id == first.conversation_id
    turns = bob.memory.recent_turns(first.conversation_id)
    assert [(t.role, t.content) for t in turns] == [
        ("user", "remember the plan"),
        ("assistant", "ok"),
        ("user", "and then?"),
        ("assistant", "second answer"),
    ]


async def test_explicit_remember_requests_become_profile_facts(bob, fake_ollama, model):
    fake_ollama.route = {
        "domain": "notes", "needs_tools": False, "risk": "low",
        "memory_fact": "Acme reports use the Q3 template",
    }  # fmt: skip
    await bob.orchestrator.handle("remember that Acme reports use the Q3 template")
    facts = bob.memory.facts()
    assert [f.fact for f in facts] == ["Acme reports use the Q3 template"]
    assert facts[0].source.startswith("conversation ")
    fake_ollama.route = {
        "domain": "notes",
        "needs_tools": False,
        "risk": "low",
        "memory_fact": None,
    }
    await bob.orchestrator.handle("hello")
    assert "Acme reports use the Q3 template" in model.seen_instructions[-1]
    assert bob.memory.delete_fact(facts[0].id) and bob.memory.facts() == []


async def test_denied_tool_is_reported_to_the_model_not_run(bob, model):
    model.steps = [("notes_send", {"to": "x@y.z"}), "I could not send it."]
    reply = await bob.orchestrator.handle("send my notes to x@y.z")  # not interactive
    assert reply.tools[0].outcome == "denied" and reply.tools[0].result["denied"] is True
    assert reply.text == "I could not send it."
    assert bob.audit.entries(kind="tool")[0].outcome == "denied"


async def test_the_loop_stops_after_the_step_limit(bob, model):
    model.steps = [("notes_add", {"text": "again"})]  # never produces an answer
    reply = await bob.orchestrator.handle("loop forever")
    assert "stopped after 8 steps" in reply.text and reply.steps == 8


async def test_router_failure_falls_back_to_keywords(bob, fake_ollama, model):
    fake_ollama.fail_chat = True
    reply = await bob.orchestrator.handle("add a note about lunch")
    assert reply.specialist == "notes" and reply.route.routed_by == "keywords"


async def test_no_specialist_means_a_plain_tool_less_answer(bob, model):
    await bob.plugins.disable("notes")
    reply = await bob.orchestrator.handle("what is a monad?")
    assert reply.specialist == "general" and model.seen_tools[-1] == []


async def test_finished_tasks_become_episodes(bob, model):
    model.steps = [("notes_add", {"text": "buy milk"}), "Added."]
    await bob.orchestrator.handle("add a note to buy milk")
    row = bob.db.query_one("SELECT * FROM episodes")
    assert row["specialist"] == "notes" and "notes.add" in row["summary"]
    assert row["embedding"] is not None
    related = await bob.memory.related_episodes("add a note to buy milk", min_score=0.0)
    assert related and related[0].summary == row["summary"]


def test_verify_flags_uncited_and_failed_validation():
    state = TurnState(caller=Caller(kind="model", name="x", plugin="forms"))
    assert verify(" plain answer ", state, "forms") == ("plain answer", [])
    state.sources.append(Source(1, "a:b:1@c", "a", "s", "b", 1, "c", "t"))
    assert verify("Cited [1].", state, "forms")[1] == []
    assert "did not cite" in verify("No citation.", state, "forms")[1][0]
    assert "not retrieved" in verify("Cited [1] and [7].", state, "forms")[1][0]
    state.tools.append(ToolRecord("forms", "validate", {}, "ok", {"ok": False, "errors": ["x"]}))
    assert any("Validation did not pass" in w for w in verify("Done [1].", state, "forms")[1])
    state.tools.append(ToolRecord("forms", "validate", {}, "ok", {"ok": True}))
    assert verify("Done [1].", state, "forms")[1] == []


def test_source_numbers_are_stable_across_searches():
    state = TurnState(caller=Caller(kind="model", name="x"))
    hit = {"citation": "a:b:1@c", "collection": "a", "path": "b", "line": 1, "version": "c"}
    first = state.number_sources({"found": True, "results": [hit]})
    again = state.number_sources({"found": True, "results": [{**hit}, {**hit, "citation": "z"}]})
    assert first["results"][0]["n"] == 1
    assert [r["n"] for r in again["results"]] == [1, 2]
    assert state.number_sources("text") == "text"


def test_tool_ranking_prefers_own_plugin_then_overlap():
    candidates = [
        ("knowledge", "search", "Search one collection"),
        ("mail", "list_new", "List new mail"),
        ("mail", "search", "Search mail"),
        ("tasks", "create", "Create a task"),
    ]
    order = rank_tools("search my mail for the invoice", candidates, "mail")
    assert order[:2] == [2, 1] and order[2] == 0
    assert exposed_name("form-author", "validate") == "form_author_validate"


def test_keyword_route_uses_manifest_hints():
    from bob_sdk.manifest import SpecialistSpec

    def make(plugin_id, routes):
        spec = SpecialistSpec(prompt="p.md", routes=routes)
        return Specialist(plugin_id, plugin_id, "", spec, "", (plugin_id,))

    specialists = [
        make("mail", ["mail", "email", "inbox"]),
        make("form-author", ["form", "build form"]),
    ]
    assert keyword_route("any new email in my inbox?", specialists).domain == "mail"
    assert keyword_route("build me a form for onboarding", specialists).domain == "form-author"
    assert keyword_route("what's the weather", specialists) == Route(
        domain="general", routed_by="default"
    )


async def test_placeholders_pass_the_whole_request_without_the_model_copying_it(bob, model):
    long_request = "add this note: " + "a very long form description. " * 400
    model.steps = [("notes_add", {"text": "@request"}), "Added."]
    reply = await bob.orchestrator.handle(long_request)
    record = reply.tools[0]
    # The model's call stays short; the tool got the user's message in full.
    assert record.arguments == {"text": "@request"} and record.outcome == "ok"
    listed = await bob.broker.call("notes", "list_notes", {}, Caller(kind="user", name="user"))
    assert listed["notes"] == [long_request.strip()]


def test_placeholders_only_replace_whole_values():
    state = TurnState(caller=Caller(kind="user", name="user"), request="R", conversation="c1")
    filled = state.fill_placeholders(
        {"a": "@request", "b": " @conversation ", "c": "see @request", "d": 3}
    )
    assert filled == {"a": "R", "b": "c1", "c": "see @request", "d": 3}


async def test_a_specialist_gets_its_tools_even_when_the_router_says_no_lookups(
    bob, fake_ollama, model
):
    # The router model thinks building something needs no lookups...
    fake_ollama.route = {
        "domain": "notes",
        "needs_tools": False,
        "risk": "low",
        "memory_fact": None,
    }
    model.steps = [("notes_add", {"text": "@request"}), "Added."]
    reply = await bob.orchestrator.handle("add a note saying hello to everyone in the team")
    # ...but the specialist still sees and uses its tools.
    assert [(t.plugin, t.tool) for t in reply.tools] == [("notes", "add")]
    assert "notes_add" in model.seen_tools[0]
