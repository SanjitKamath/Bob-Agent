"""Fast paths and the answer cache."""

from __future__ import annotations

import pytest
from bobd.cache import is_self_contained
from bobd.router import is_small_talk, keyword_route


@pytest.mark.parametrize(
    "text",
    ["Hi", "hi bob", "Hello there!", "Good morning Bob", "thanks!", "Thank you so much", "bye"],
)
def test_greetings_and_thanks_are_small_talk(text):
    assert is_small_talk(text)


@pytest.mark.parametrize(
    "text",
    [
        "Hi Bob, what are my latest emails?",
        "thanks, now add milk to my tasks",
        "hello world program in python",
        "",
    ],
)
def test_requests_are_not_small_talk(text):
    assert not is_small_talk(text)


def test_keyword_routes_match_plurals(bob):
    specialists = bob.orchestrator.specialists()
    assert keyword_route("show me my notes", specialists).domain == "notes"
    assert keyword_route("any new note?", specialists).domain == "notes"


async def test_small_talk_is_answered_at_once_without_any_model(bob, model, fake_ollama):
    reply = await bob.orchestrator.handle("Hi Bob")
    assert reply.route.routed_by == "quick" and reply.route.small_talk
    assert reply.steps == 0 and reply.text.startswith(("Hi!", "Hello!", "Hey!"))
    assert "I can help with" in reply.text  # the first greeting says what Bob does
    assert model.seen_tools == [] and fake_ollama.chat_requests == []  # no model at all
    assert set(reply.timings) == {"total"} and reply.seconds < 1
    thanks = await bob.orchestrator.handle("thanks!", conversation_id=reply.conversation_id)
    assert thanks.text in ("You're welcome!", "Any time.", "Glad that helped.")
    assert fake_ollama.chat_requests == []


async def test_small_talk_can_still_be_written_by_the_router_model(bob, model, fake_ollama):
    bob.config.agent.small_talk = "model"
    reply = await bob.orchestrator.handle("Hi Bob")
    assert reply.text == "a plain answer" and reply.steps == 0
    assert model.seen_tools == []  # the main model never ran
    assert len(fake_ollama.chat_requests) == 1
    assert "format" not in fake_ollama.chat_requests[0]  # no routing call
    assert set(reply.timings) == {"model", "total"}


@pytest.mark.parametrize(
    ("text", "reusable"),
    [
        ("What is a monad?", True),
        ("How do I write a spreadsheet formula for a hidden column?", True),
        ("what about it?", False),
        ("and the second one?", False),
        ("Why?", False),
        ("explain that again", False),
        ("Can you make it shorter", False),
        ("ok", False),
    ],
)
def test_only_questions_that_stand_alone_are_reused_mid_conversation(text, reusable):
    assert is_self_contained(text) is reusable


async def test_a_repeated_question_is_reused_in_the_middle_of_a_conversation(bob, model):
    model.steps = ["A monad is a wrapper with bind."]
    await bob.orchestrator.handle("What is a monad?")
    other = await bob.orchestrator.handle("What is a functor exactly?")
    again = await bob.orchestrator.handle("what is a MONAD", conversation_id=other.conversation_id)
    assert again.cached and again.text == "A monad is a wrapper with bind."
    follow_up = await bob.orchestrator.handle(
        "and what is it for?", conversation_id=other.conversation_id
    )
    assert not follow_up.cached


async def test_models_that_do_not_fit_together_are_not_reloaded_forever(bob, fake_ollama):
    skipped: set[str] = set()
    # Ollama reports only the router loaded: main was pushed out right after loading.
    await bob._check_fit({"main": "bob-main-alias", "router": "router-model"}, skipped)
    assert skipped == {"main"}
    assert "do not all fit in memory" in bob.models.memory_note
    assert "do not all fit" in (await bob.models.health())["memory"]
    fine: set[str] = set()
    bob.models.memory_note = ""
    await bob._check_fit({"router": "router-model"}, fine)
    assert fine == set() and bob.models.memory_note == ""


async def test_a_switched_off_plugin_is_named_instead_of_guessed(bob, model):
    await bob.plugins.disable("notes")
    reply = await bob.orchestrator.handle("What are my latest notes?")
    assert "Notes plugin isn't running" in reply.text
    assert "bob plugin enable notes" in reply.text
    assert model.seen_tools == []


async def test_no_tools_needed_means_no_tools_sent(bob, model, fake_ollama):
    # A general question that needs no lookups: no tools, so the prompt stays short.
    fake_ollama.route = {**fake_ollama.route, "domain": "general", "needs_tools": False}
    await bob.orchestrator.handle("What does idempotent mean?")
    assert model.seen_tools == [[]]


async def test_a_repeated_question_is_answered_from_the_cache(bob, model):
    model.steps = ["A monad is a wrapper with bind."]
    first = await bob.orchestrator.handle("What is a monad?")
    assert not first.cached
    again = await bob.orchestrator.handle("what is a MONAD")
    assert again.cached and again.cached_at
    assert again.text == "A monad is a wrapper with bind."
    assert again.route.routed_by == "cache" and "cache" in again.timings
    assert len(model.seen_tools) == 1  # the model ran once
    assert bob.answer_cache.stats()["hits"] == 1


async def test_fresh_skips_the_cache_and_a_follow_up_is_not_reused(bob, model):
    first = await bob.orchestrator.handle("What is a monad?")
    await bob.orchestrator.handle("What is a monad?", fresh=True)
    assert len(model.seen_tools) == 2
    # The same question again, mid-conversation: it stands on its own, so it is reused.
    same = await bob.orchestrator.handle("What is a monad?", conversation_id=first.conversation_id)
    assert same.cached and len(model.seen_tools) == 2
    # A follow-up leans on the conversation: worked out again.
    await bob.orchestrator.handle("What is it used for?", conversation_id=first.conversation_id)
    assert len(model.seen_tools) == 3


async def test_answers_from_live_tools_are_never_cached(bob, model):
    model.steps = [("notes_add", {"text": "milk"}), "Added."]
    await bob.orchestrator.handle("Note down milk")
    again = await bob.orchestrator.handle("Note down milk")
    assert not again.cached


async def test_a_knowledge_answer_is_reused_until_its_collection_changes(bob, model, plugins_dir):
    model.steps = [("knowledge_search", {"collection": "notes", "query": "x"}), "It is X [1]."]
    first = await bob.orchestrator.handle("Explain X")
    assert first.sources and not first.cached
    hit = await bob.orchestrator.handle("Explain X")
    assert hit.cached and hit.sources[0].citation == first.sources[0].citation
    # Re-indexing changes the collection's version: the cached answer is dropped.
    (plugins_dir / "knowledge" / "version.txt").write_text("v2")
    miss = await bob.orchestrator.handle("Explain X")
    assert not miss.cached


def test_an_answer_that_found_nothing_is_not_kept(bob):
    # The test double always finds something, so check the rule directly.
    from bobd.agent import ToolRecord, TurnState
    from bobd.broker import Caller
    from bobd.router import Route

    state = TurnState(caller=Caller(kind="model", name="notes"))
    state.tools.append(
        ToolRecord("knowledge", "search", {"collection": "c"}, "ok", {"found": False})
    )
    assert not bob.orchestrator._cacheable(Route(domain="notes"), None, state, "ok", [])


def test_known_mail_providers_are_filled_in():
    from bobd.cli import mail_provider

    name, provider = mail_provider("someone@gmail.com")
    assert name == "gmail" and provider["imap"] == ("imap.gmail.com", 993)
    assert provider["smtp"] == ("smtp.gmail.com", 465)
    assert mail_provider("a@icloud.com")[1]["smtp"] == ("smtp.mail.me.com", 587)
    assert mail_provider("a@company.in") is None


def test_a_plugin_without_an_account_shows_what_to_do_on_its_card():
    from bob_sdk.manifest import WidgetCard
    from bobd.cards import render

    card = WidgetCard(title="Mail", value="counts.urgent", status="{needs_reply} need a reply")
    rendered = render(
        type("M", (), {"id": "mail"})(),
        card,
        {"set_up": False, "status": "Not set up: run: bob mail setup", "counts": {}},
    )
    assert rendered["value"] is None and rendered["status"] == "Not set up: run: bob mail setup"
