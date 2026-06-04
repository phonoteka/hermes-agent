"""Behavior tests for Canon Telegram review callback semantics."""

import asyncio
import threading
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


def _outcome(
    text: str = "recorded",
    *,
    notify_chat: bool = False,
    status: str = "recorded",
    delivery_mode: str = "inline_only",
) -> dict:
    return {
        "text": text,
        "notify_chat": notify_chat,
        "status": status,
        "delivery_mode": delivery_mode,
    }


class _AuthRunner:
    def _is_user_authorized(self, source):
        return True

    async def _handle_message(self, event):
        return None


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = AsyncMock()
    adapter._bot.username = "test_bot"
    adapter._app = MagicMock()
    runner = _AuthRunner()
    adapter._message_handler = runner._handle_message
    return adapter


def _make_pending_revise(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "chat_id": "12345",
        "thread_id": "999",
        "prompt_message_id": "777",
    }


def _arm_pending_revise(adapter: TelegramAdapter, run_id: str) -> str:
    """Store pending revise state using the adapter's same-origin key semantics.

    pre: adapter exposes `_canon_revise_origin_key` and `_canon_pending_revise`.
    post: returns the exact dictionary key the adapter will later use for same-origin lookup.
    post: adapter pending state contains one revise entry for `run_id` under that derived key.
    raises: none.
    """

    pending = _make_pending_revise(run_id)
    origin_key = adapter._canon_revise_origin_key(
        chat_id=pending["chat_id"],
        thread_id=pending["thread_id"],
    )
    adapter._canon_pending_revise[origin_key] = pending
    return origin_key


def _make_text_update(*, update_id: int, message_id: int, text: str):
    update = MagicMock()
    update.update_id = update_id
    update.message = MagicMock()
    update.message.chat_id = 12345
    update.message.message_id = message_id
    update.message.message_thread_id = 999
    update.message.chat.type = "supergroup"
    update.message.from_user.id = 333
    update.message.from_user.first_name = "Operator"
    update.message.text = text
    return update


async def _assert_event_set(event: threading.Event, *, timeout: float, failure_message: str) -> None:
    """Fail fast when a blocking resolver test never reaches its synchronization point.

    pre: the patched resolver must set `event` before blocking on the release gate.
    post: returns only after the resolver thread reached the expected wait boundary.
    raises: AssertionError when the resolver never started within `timeout` seconds.
    """

    assert await asyncio.to_thread(event.wait, timeout), failure_message


async def _await_adapter_background_tasks(adapter: TelegramAdapter) -> None:
    tasks = list(getattr(adapter, "_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_revise_button_waits_for_next_authorized_message():
    """AC-S3-003: revise click must arm pending state and avoid immediate decision record.

    pre: authorized operator presses `cg:e:<run_id>` on an active review card.
    post: callback stores pending same-origin revise capture state and does not call
          resolve_telegram_canon_review on button click.
    raises: AssertionError while e-button still records a direct decision.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = "cg:e:cg-redprep-1"
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.message.message_id = 777
    query.message.message_thread_id = 999
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 222
    query.from_user.first_name = "Breanainn"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    assert resolver.call_count == 0
    assert len(adapter._canon_pending_revise) == 1
    assert next(iter(adapter._canon_pending_revise.values()))["run_id"] == "cg-redprep-1"


@pytest.mark.asyncio
async def test_revise_followup_same_origin_records_revision_text():
    """AC-S3-003: next authorized same-origin text must record revise with payload text.

    pre: revise callback armed pending state for one chat/thread.
    post: first authorized text message from same origin resolves Canon review with
          revision instructions and clears pending state.
    raises: AssertionError while follow-up text is ignored or missing from payload.
    """

    adapter = _make_adapter()
    _arm_pending_revise(adapter, "cg-redprep-2")

    update = MagicMock()
    update.update_id = 1
    update.message = MagicMock()
    update.message.chat_id = 12345
    update.message.message_id = 888
    update.message.message_thread_id = 999
    update.message.chat.type = "supergroup"
    update.message.from_user.id = 333
    update.message.from_user.first_name = "Operator"
    update.message.text = "Добавить SLA и риски по интеграции"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_text_message(update, MagicMock())
        await _await_adapter_background_tasks(adapter)

    resolver.assert_called_once_with(
        run_id="cg-redprep-2",
        choice="e",
        actor_id="333",
        actor_name="Operator",
        chat_id="12345",
        thread_id="999",
        message_id="777",
        revision_instructions="Добавить SLA и риски по интеграции",
        sender=ANY,
    )
    assert not adapter._canon_pending_revise


@pytest.mark.asyncio
async def test_revise_followup_sends_progress_before_slow_resolver():
    """AC-S3-003: revise follow-up must ACK progress before slow Canon resume work begins.

    pre: a same-origin revise capture is armed and the next authorized text is accepted as the
          single revision payload.
    post: Telegram sends an immediate progress ACK before invoking the slow Canon resolver; an
          inline-only resolver outcome does not force a second generic chat message.
    raises: AssertionError while the resolver starts before the progress ACK reaches chat.
    """

    adapter = _make_adapter()
    call_order = []
    _arm_pending_revise(adapter, "cg-redprep-progress")

    update = _make_text_update(update_id=10, message_id=888, text="Не pong, а pang")

    async def _send_message_side_effect(**kwargs):
        call_order.append(("send", kwargs["text"]))

    adapter._bot.send_message = AsyncMock(side_effect=_send_message_side_effect)

    def _resolver_side_effect(**kwargs):
        call_order.append(("resolver", kwargs["revision_instructions"]))
        return _outcome()

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=_resolver_side_effect):
        await adapter._handle_text_message(update, MagicMock())
        await _await_adapter_background_tasks(adapter)

    assert call_order[0][0] == "send"
    assert "Canon" in call_order[0][1]
    assert call_order[1] == ("resolver", "Не pong, а pang")
    assert call_order == [
        ("send", "Canon: коррективы записаны; workflow продолжает работу отдельно, чат свободен."),
        ("resolver", "Не pong, а pang"),
    ]


@pytest.mark.asyncio
async def test_revise_followup_nonblocking_failure_notifies_asynchronously_without_rearming():
    """Revise failure contract must return immediately and notify later from background work.

    pre: first same-origin revise follow-up was captured and the durable resolver is still blocked.
    post: handler returns before resolver completion, pending revise stays consumed, and background
          failure sends an operator notice without re-arming revise interception.
    raises: AssertionError while the first follow-up blocks on resolver completion instead of
            returning immediately.
    """

    adapter = _make_adapter()
    adapter._enqueue_text_event = MagicMock()
    _arm_pending_revise(adapter, "cg-redprep-failure")
    resolver_started = threading.Event()
    release_resolver = threading.Event()

    update = _make_text_update(update_id=11, message_id=889, text="Не pong, а pang")

    def _resolver_side_effect(**kwargs):
        resolver_started.set()
        assert kwargs["revision_instructions"] == "Не pong, а pang"
        assert release_resolver.wait(timeout=2)
        raise RuntimeError("boom")

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=_resolver_side_effect):
        first_task = asyncio.create_task(adapter._handle_text_message(update, MagicMock()))
        try:
            await _assert_event_set(
                resolver_started,
                timeout=2,
                failure_message="resolver did not reach the blocking synchronization point",
            )
            assert not adapter._canon_pending_revise
            assert first_task.done(), (
                "first revise follow-up should return immediately and let failure notification run "
                "asynchronously; current code still blocks until resolver completion"
            )
            assert len(adapter._background_tasks) == 1
        finally:
            release_resolver.set()
            await first_task
            await _await_adapter_background_tasks(adapter)

    adapter._enqueue_text_event.assert_not_called()
    sent_texts = [call.kwargs.get("text", "") for call in adapter._bot.send_message.await_args_list]
    assert any("не записался" in text for text in sent_texts)
    assert not adapter._background_tasks

    next_update = MagicMock()
    next_update.update_id = 12
    next_update.message = MagicMock()
    next_update.message.chat_id = 12345
    next_update.message.message_id = 890
    next_update.message.message_thread_id = 999
    next_update.message.chat.type = "supergroup"
    next_update.message.from_user.id = 333
    next_update.message.from_user.first_name = "Operator"
    next_update.message.text = "обычный следующий вопрос"

    await adapter._handle_text_message(next_update, MagicMock())

    adapter._enqueue_text_event.assert_called_once()


@pytest.mark.asyncio
async def test_revise_followup_nonblocking_returns_before_slow_resolver_completion():
    """One-shot revise capture must return before long-running resume finishes.

    pre: first same-origin revise follow-up is already claimed and the durable resolver is blocked.
    post: handler returns immediately after the progress ACK, while the resolver keeps running in
          background and the chat is free for the next ordinary message.
    raises: AssertionError while the first follow-up still blocks on resolver completion.
    """

    adapter = _make_adapter()
    adapter._enqueue_text_event = MagicMock()
    origin_key = _arm_pending_revise(adapter, "cg-redprep-race")
    resolver_started = threading.Event()
    release_resolver = threading.Event()

    first_update = _make_text_update(update_id=20, message_id=901, text="первый revise")
    second_update = _make_text_update(update_id=21, message_id=902, text="второе обычное сообщение")
    resolver_inputs = []

    def _resolver_side_effect(**kwargs):
        resolver_started.set()
        resolver_inputs.append(kwargs["revision_instructions"])
        assert release_resolver.wait(timeout=2)
        return _outcome("recorded")

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=_resolver_side_effect):
        first_task = asyncio.create_task(adapter._handle_text_message(first_update, MagicMock()))
        try:
            await _assert_event_set(
                resolver_started,
                timeout=2,
                failure_message="resolver did not reach the blocking synchronization point",
            )
            assert first_task.done(), (
                "first revise follow-up should return immediately after the ACK; current code "
                "still blocks until resolver completion"
            )
            assert len(adapter._background_tasks) == 1

            await adapter._handle_text_message(second_update, MagicMock())
            adapter._enqueue_text_event.assert_called_once()
            assert resolver_inputs == ["первый revise"]
            assert origin_key not in adapter._canon_pending_revise
        finally:
            release_resolver.set()
            await first_task
            await _await_adapter_background_tasks(adapter)

    assert not adapter._background_tasks


@pytest.mark.asyncio
async def test_revise_followup_slow_resolver_only_captures_first_text_even_if_second_message_arrives():
    """Blocked resolver still receives only the first revise text.

    pre: first same-origin revise follow-up is already claimed and resolver completion is delayed.
    post: pending revise is consumed by the first text only; a later same-origin message is routed
          as normal chat when the handler is invoked again and never reaches the resolver.
    raises: AssertionError while revise capture leaks the second message into the resolver.
    """

    adapter = _make_adapter()
    adapter._enqueue_text_event = MagicMock()
    _arm_pending_revise(adapter, "cg-redprep-race")
    resolver_started = threading.Event()
    release_resolver = threading.Event()

    first_update = _make_text_update(update_id=20, message_id=901, text="первый revise")
    second_update = _make_text_update(update_id=21, message_id=902, text="второе обычное сообщение")
    resolver_inputs = []

    def _resolver_side_effect(**kwargs):
        resolver_started.set()
        resolver_inputs.append(kwargs["revision_instructions"])
        assert release_resolver.wait(timeout=2)
        return _outcome("recorded")

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=_resolver_side_effect):
        first_task = asyncio.create_task(adapter._handle_text_message(first_update, MagicMock()))
        try:
            await _assert_event_set(
                resolver_started,
                timeout=2,
                failure_message="resolver did not reach the blocking synchronization point",
            )
            await adapter._handle_text_message(second_update, MagicMock())
            adapter._enqueue_text_event.assert_called_once()
            assert resolver_inputs == ["первый revise"]
            assert not adapter._canon_pending_revise
        finally:
            release_resolver.set()
            await first_task
            await _await_adapter_background_tasks(adapter)

    assert not adapter._background_tasks


@pytest.mark.asyncio
async def test_revise_followup_wrong_origin_is_ignored_fail_closed():
    """AC-S3-003: wrong-origin follow-up must not satisfy pending revise capture.

    pre: pending revise state exists for chat/thread A.
    post: message from chat/thread B does not call resolver and pending state remains.
    raises: AssertionError while wrong-origin text can resolve revise.
    """

    adapter = _make_adapter()
    _arm_pending_revise(adapter, "cg-redprep-3")

    update = MagicMock()
    update.update_id = 2
    update.message = MagicMock()
    update.message.chat_id = 99999
    update.message.message_id = 889
    update.message.message_thread_id = 1
    update.message.chat.type = "supergroup"
    update.message.from_user.id = 333
    update.message.from_user.first_name = "Operator"
    update.message.text = "чужой тред"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_text_message(update, MagicMock())

    resolver.assert_not_called()
    assert len(adapter._canon_pending_revise) == 1
    assert next(iter(adapter._canon_pending_revise.values()))["run_id"] == "cg-redprep-3"


@pytest.mark.asyncio
async def test_canon_callback_preserves_thread_identity():
    """AC-S4-002 RED guard: callback resolver must receive Telegram thread identity.

    pre: authorized operator presses approve callback on a topic-bound review card.
    post: Telegram adapter forwards chat/message/thread ids from callback message into resolver.
    raises: AssertionError while callback thread identity is dropped or rewritten.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = "cg:y:cg-s4-run"
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    resolver.assert_called_once_with(
        run_id="cg-s4-run",
        choice="y",
        actor_id="333",
        actor_name="Operator",
        chat_id="-10012345",
        thread_id="789",
        message_id="456",
    )


@pytest.mark.asyncio
async def test_canon_callback_requires_gate_action_and_message_identity():
    """AC-R05-002 RED: callback resolver must bind gate/action plus exact Telegram origin.

    pre: an authorized operator clicks a projected Canon review button whose callback
         payload carries gate identity instead of legacy `cg:y/n/e:<run>` data.
    post: Telegram callback handling forwards gateId/action and chat/thread/message
          identity into the resolver so Canon can fail closed on mismatches/replay.
    raises: AssertionError while the adapter still ignores projected gate/action
            callbacks or drops message identity.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = '{"gateId":"gate-r05","action":"approve"}'
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    resolver.assert_called_once_with(
        gate_id="gate-r05",
        action_id="approve",
        actor_id="333",
        actor_name="Operator",
        chat_id="-10012345",
        thread_id="789",
        message_id="456",
    )


@pytest.mark.asyncio
async def test_canon_non_revise_callback_answers_before_resolver_runs():
    """Non-revise Canon callbacks ACK Telegram before durable resolver work starts."""

    adapter = _make_adapter()
    call_order = []

    query = AsyncMock()
    query.data = '{"gateId":"gate-r05","action":"approve"}'
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"

    async def _answer_side_effect(*args, **kwargs):
        call_order.append("answer")

    query.answer = AsyncMock(side_effect=_answer_side_effect)
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    def _resolver_side_effect(**kwargs):
        call_order.append("resolver")
        return _outcome()

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=_resolver_side_effect):
        await adapter._handle_callback_query(update, MagicMock())

    assert call_order[:2] == ["answer", "resolver"]


@pytest.mark.asyncio
async def test_canon_non_revise_callback_replaces_buttons_with_fixed_choice_panel_while_processing():
    """Callback click must immediately remove old buttons and show a fixed choice + processing panel.

    pre: operator clicks approve/reject callback for a Canon review card with inline buttons.
    post: adapter first edits the original message with reply_markup=None and a fixed-choice
          status panel before calling the resolver; final edit can include resolver output.
    raises: AssertionError while old buttons stay active until resolver completion.
    """

    adapter = _make_adapter()
    call_order = []

    query = AsyncMock()
    query.data = '{"gateId":"gate-r05","action":"approve"}'
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()

    async def _edit_message_text_side_effect(*args, **kwargs):
        call_order.append(("edit", kwargs.get("text", ""), kwargs.get("reply_markup", "<missing>")))

    query.edit_message_text = AsyncMock(side_effect=_edit_message_text_side_effect)

    update = MagicMock()
    update.callback_query = query

    def _resolver_side_effect(**kwargs):
        call_order.append(("resolver", kwargs.get("action_id") or kwargs.get("choice")))
        return _outcome()

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=_resolver_side_effect):
        await adapter._handle_callback_query(update, MagicMock())

    assert call_order[0][0] == "edit"
    assert "Выбор зафиксирован" in call_order[0][1]
    assert "✅ Да" in call_order[0][1]
    assert "Обрабатываю" in call_order[0][1]
    assert call_order[0][2] is None
    assert call_order[1] == ("resolver", "approve")


@pytest.mark.asyncio
async def test_completed_canon_callback_sends_operator_closeout_as_new_message():
    """Completed Canon reviews must keep the old card compact and send one fresh closeout.

    pre: approve callback resolves to a completed current-gateway closeout with artifact refs.
    post: Telegram adapter edits the original card with compact status only and sends the full
          closeout exactly once as a fresh message in the same chat/thread.
    raises: AssertionError while terminal closeout text is duplicated into the edited old card.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = '{"a":"approve","n":"review","r":"sm-closeout","t":"0"}'
    query.message = MagicMock()
    query.message.chat_id = 5558998798
    query.message.message_id = 8060
    query.message.message_thread_id = None
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = 5558998798
    query.from_user.first_name = "Breanainn"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    closeout_text = (
        "Canon review recorded: `completed` for `sm-closeout`.\n"
        "Кратко: done\n"
        "Пакет спеков: `/tmp/spec-package`\n"
        "План: `/tmp/implementation-plan.md`"
    )

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=_outcome(closeout_text, notify_chat=True, status="completed", delivery_mode="fresh_closeout"),
    ):
        await adapter._handle_callback_query(update, MagicMock())

    assert query.edit_message_text.call_count == 2
    first_edit = query.edit_message_text.call_args_list[0].kwargs
    assert first_edit["reply_markup"] is None
    assert "Выбор зафиксирован: ✅ Да" in first_edit["text"]
    assert "Статус: ⏳ Обрабатываю" in first_edit["text"]

    final_edit = query.edit_message_text.call_args_list[-1].kwargs
    assert final_edit["reply_markup"] is None
    assert "Статус: ✅ Завершено" in final_edit["text"]
    assert closeout_text not in final_edit["text"]

    adapter._bot.send_message.assert_called_once()
    sent = adapter._bot.send_message.call_args.kwargs
    assert sent["chat_id"] == 5558998798
    assert sent["text"] == closeout_text
    assert sent.get("parse_mode") is None


@pytest.mark.asyncio
async def test_paused_again_canon_callback_can_send_explicit_status_message():
    """Structured nonterminal outcomes can still emit a fresh operator status message.

    pre: approve/revise callback resolves into a paused-again current-gateway state.
    post: Telegram sends the explicit status update as a fresh message because the resolver
          returned structured notify_chat authority, not because the text matched any heuristic.
    raises: AssertionError while nonterminal workflow updates stay hidden in the edited card.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = '{"a":"approve","n":"review","r":"sm-paused-again"}'
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    status_text = "Canon review recorded: `awaiting-human-review` for `sm-paused-again`."

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=_outcome(
            status_text,
            notify_chat=True,
            status="awaiting-human-review",
            delivery_mode="fresh_status",
        ),
    ):
        await adapter._handle_callback_query(update, MagicMock())

    final_edit = query.edit_message_text.call_args_list[-1].kwargs
    assert final_edit["reply_markup"] is None
    assert "Статус: `awaiting-human-review`" in final_edit["text"]
    assert status_text not in final_edit["text"]

    adapter._bot.send_message.assert_called_once()
    sent = adapter._bot.send_message.call_args.kwargs
    assert sent["chat_id"] == -10012345
    assert sent["text"] == status_text
    assert sent.get("parse_mode") is None


@pytest.mark.asyncio
async def test_callback_review_card_only_skips_fresh_message_even_when_notify_chat_true():
    """Callback transport must trust explicit review-card-only delivery authority.

    pre: resolver returns contradictory fields where legacy notify_chat says to send, but
         delivery_mode says the next review card already owns delivery.
    post: Telegram sends no fresh message and keeps only the compact card edit outcome.
    raises: AssertionError while callback delivery still branches on notify_chat alone.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = '{"a":"approve","n":"review","r":"sm-review-card-only"}'
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=_outcome(
            "Canon review recorded: `awaiting-human-review` for `sm-review-card-only`.",
            notify_chat=True,
            status="awaiting-human-review",
            delivery_mode="review_card_only",
        ),
    ):
        await adapter._handle_callback_query(update, MagicMock())

    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_callback_missing_delivery_mode_fails_closed_even_when_notify_chat_true():
    """Callback transport must not use legacy notify_chat without delivery authority.

    pre: resolver returns no delivery_mode but legacy notify_chat asks for a fresh message.
    post: Telegram sends no fresh message because delivery_mode is the only authoritative
          transport contract for Canon review outcomes.
    raises: AssertionError while missing delivery_mode silently falls back to notify_chat.
    """

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = '{"a":"approve","n":"review","r":"sm-missing-delivery"}'
    query.message = MagicMock()
    query.message.chat_id = -10012345
    query.message.message_id = 456
    query.message.message_thread_id = 789
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    outcome = _outcome(
        "Canon review recorded: `completed` for `sm-missing-delivery`.",
        notify_chat=True,
        status="completed",
        delivery_mode="fresh_closeout",
    )
    outcome.pop("delivery_mode")

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=outcome,
    ):
        await adapter._handle_callback_query(update, MagicMock())

    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_revise_background_completed_outcome_sends_one_fresh_closeout_message():
    """Background revise completion must keep one terminal closeout message.

    pre: one-shot revise capture already sent its immediate ACK and the async resolver returns a
         completed closeout.
    post: background delivery emits exactly one fresh closeout message and does not duplicate it
          through any second status send.
    raises: AssertionError while revise completion emits multiple fresh closeout messages.
    """

    adapter = _make_adapter()
    closeout_text = (
        "Canon review recorded: `completed` for `sm-closeout`.\n"
        "Кратко: done\n"
        "Пакет спеков: `/tmp/spec-package`\n"
        "План: `/tmp/implementation-plan.md`"
    )

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=_outcome(closeout_text, notify_chat=True, status="completed", delivery_mode="fresh_closeout"),
    ):
        await adapter._run_pending_canon_revise_resolution(
            chat_id="12345",
            thread_id="999",
            resolver_kwargs={"run_id": "sm-closeout", "choice": "e", "actor_id": "333", "message_id": "777"},
        )

    adapter._bot.send_message.assert_called_once()
    sent = adapter._bot.send_message.call_args.kwargs
    assert sent["chat_id"] == 12345
    assert sent["message_thread_id"] == 999
    assert sent["text"] == closeout_text
    assert sent.get("parse_mode") is None


@pytest.mark.asyncio
async def test_revise_background_fresh_status_sends_message_even_when_notify_chat_false():
    """Background revise transport must trust explicit fresh-status delivery authority.

    pre: resolver returns contradictory fields where notify_chat is false but delivery_mode says
         a fresh status message must be delivered.
    post: Telegram still sends exactly one fresh status message.
    raises: AssertionError while background revise delivery still branches on notify_chat alone.
    """

    adapter = _make_adapter()
    status_text = (
        "Canon review recorded: `awaiting-human-review` for `sm-paused-again`.\n"
        "Кратко: summary [needs review] (/tmp/spec-package)"
    )

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=_outcome(
            status_text,
            notify_chat=False,
            status="awaiting-human-review",
            delivery_mode="fresh_status",
        ),
    ):
        await adapter._run_pending_canon_revise_resolution(
            chat_id="12345",
            thread_id="999",
            resolver_kwargs={"run_id": "sm-paused-again", "choice": "e", "actor_id": "333", "message_id": "777"},
        )

    adapter._bot.send_message.assert_called_once()
    sent = adapter._bot.send_message.call_args.kwargs
    assert sent["chat_id"] == 12345
    assert sent["message_thread_id"] == 999
    assert sent["text"] == status_text
    assert sent.get("parse_mode") is None


@pytest.mark.asyncio
async def test_revise_background_review_card_only_outcome_skips_generic_status_message():
    """Background revise delivery must honor structured review-card-only authority.

    pre: revise resume already delivered the next review card through the sender seam and the
         resolver returns a review-card-only outcome with human-readable status text.
    post: Telegram sends no extra generic status message, proving transport follows explicit
          delivery authority instead of parsing the text body.
    raises: AssertionError while background revise still emits a heuristic-based status notice.
    """

    adapter = _make_adapter()
    status_text = "Canon review recorded: `awaiting-human-review` for `sm-paused-again`."

    with patch(
        "tools.canon_gateway_review.resolve_telegram_canon_review_outcome",
        return_value=_outcome(
            status_text,
            notify_chat=False,
            status="awaiting-human-review",
            delivery_mode="review_card_only",
        ),
    ):
        await adapter._run_pending_canon_revise_resolution(
            chat_id="12345",
            thread_id="999",
            resolver_kwargs={"run_id": "sm-paused-again", "choice": "e", "actor_id": "333", "message_id": "777"},
        )

    adapter._bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_compact_canon_callback_reconstructs_gate_identity_under_telegram_limit():
    """Compact Canon callbacks must fit Telegram while preserving resolvable gate authority.

    pre: projected callback data contains compact action/node/run authority and is clicked in a topic.
    post: the resolver receives the full gate id reconstructed from callback plus Telegram origin identity.
    raises: AssertionError while compact callbacks exceed Telegram's limit or lose gate identity.
    """

    adapter = _make_adapter()
    callback_data = '{"a":"approve","n":"review","r":"sm-260526185203"}'
    assert len(callback_data.encode("utf-8")) <= 64
    query = AsyncMock()
    query.data = callback_data
    query.message = MagicMock()
    query.message.chat_id = -1003351905082
    query.message.message_id = 457
    query.message.message_thread_id = 25613
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    resolver.assert_called_once_with(
        gate_id="sm-260526185203:telegram:-1003351905082:25613:review",
        action_id="approve",
        actor_id="333",
        actor_name="Operator",
        chat_id="-1003351905082",
        thread_id="25613",
        message_id="457",
    )


@pytest.mark.asyncio
async def test_tokenized_canon_callback_passes_opaque_authority_to_resolver_under_telegram_limit():
    """Tokenized Canon callbacks keep long run ids out of Telegram callback_data.

    pre: projected callback data contains only action plus durable callback token authority.
    post: the gateway passes the token and Telegram origin identity to the non-agentic resolver.
    raises: AssertionError while tokenized callbacks are ignored or expanded in Telegram callback_data.
    """

    adapter = _make_adapter()
    callback_data = '{"a":"approve","k":"cgcb_abc123"}'
    assert len(callback_data.encode("utf-8")) <= 64
    query = AsyncMock()
    query.data = callback_data
    query.message = MagicMock()
    query.message.chat_id = -1003351905082
    query.message.message_id = 457
    query.message.message_thread_id = 21676
    query.message.chat.type = "supergroup"
    query.from_user = MagicMock()
    query.from_user.id = 333
    query.from_user.first_name = "Operator"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    resolver.assert_called_once_with(
        callback_token="cgcb_abc123",
        action_id="approve",
        actor_id="333",
        actor_name="Operator",
        chat_id="-1003351905082",
        thread_id="21676",
        message_id="457",
    )


@pytest.mark.asyncio
async def test_tokenized_dm_canon_callback_preserves_embedded_transport_thread_for_no_topic_chat():
    """DM token callbacks must keep Canon transport thread authority even without Telegram topic ids.

    pre: callback_data carries an opaque callback token plus embedded Canon transport thread for a
         DM-delivered review card; Telegram callback messages in DMs expose no message_thread_id.
    post: Telegram adapter forwards the embedded transport thread to the resolver unchanged.
    raises: AssertionError while DM token callbacks drop thread identity and break resume.
    """

    adapter = _make_adapter()
    callback_data = '{"a":"approve","k":"cgcb_dm123","t":"telegram:5558998798"}'
    assert len(callback_data.encode("utf-8")) <= 64
    query = AsyncMock()
    query.data = callback_data
    query.message = MagicMock()
    query.message.chat_id = 5558998798
    query.message.message_id = 8805
    query.message.message_thread_id = None
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = 5558998798
    query.from_user.first_name = "Breanainn"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    resolver.assert_called_once_with(
        callback_token="cgcb_dm123",
        action_id="approve",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id="telegram:5558998798",
        message_id="8805",
    )


@pytest.mark.asyncio
async def test_compact_canon_callback_uses_embedded_thread_token_for_dm_sentinel():
    """Compact callbacks carry the Canon transport thread when Telegram callback lacks topic id.

    pre: a DM-delivered review was launched through the local-launch sentinel thread "1";
         Telegram callback messages in DMs have no message_thread_id.
    post: resolver still receives gate/thread identity matching the delivered Canon review evidence.
    raises: AssertionError while DM callbacks cannot reconstruct gate/thread authority.
    """

    adapter = _make_adapter()
    callback_data = '{"a":"approve","n":"review","r":"sm-260526223122","t":"1"}'
    assert len(callback_data.encode("utf-8")) <= 64
    query = AsyncMock()
    query.data = callback_data
    query.message = MagicMock()
    query.message.chat_id = 5558998798
    query.message.message_id = 457
    query.message.message_thread_id = None
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = 5558998798
    query.from_user.first_name = "Breanainn"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    resolver.assert_called_once_with(
        gate_id="sm-260526223122:telegram:5558998798:1:review",
        action_id="approve",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id="1",
        message_id="457",
    )


@pytest.mark.asyncio
async def test_dm_revise_followup_normalizes_missing_thread_and_thread_zero():
    """Revise pending state for DM/general-thread survives None vs thread_id=0 variance."""

    adapter = _make_adapter()
    query = AsyncMock()
    query.data = '{"a":"revise","n":"review","r":"sm-260526223122"}'
    query.message = MagicMock()
    query.message.chat_id = 5558998798
    query.message.message_id = 457
    query.message.message_thread_id = None
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = 5558998798
    query.from_user.first_name = "Breanainn"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_callback_query(update, MagicMock())
        resolver.assert_not_called()

    update2 = MagicMock()
    update2.update_id = 3
    update2.message = MagicMock()
    update2.message.chat_id = 5558998798
    update2.message.message_id = 888
    update2.message.message_thread_id = 0
    update2.message.chat.type = "private"
    update2.message.from_user.id = 5558998798
    update2.message.from_user.first_name = "Breanainn"
    update2.message.text = "тест"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_text_message(update2, MagicMock())
        await _await_adapter_background_tasks(adapter)

    resolver.assert_called_once_with(
        gate_id="sm-260526223122:telegram:5558998798:review",
        action_id="revise",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id="0",
        message_id="457",
        revision_instructions="тест",
        sender=ANY,
    )


@pytest.mark.asyncio
async def test_dm_revise_followup_thread_zero_does_not_consume_other_chat_origin():
    """Wrong-origin DM/general-thread follow-up remains fail-closed after thread normalization."""

    adapter = _make_adapter()
    adapter._canon_pending_revise["5558998798::"] = {
        "run_id": "sm-260526223122",
        "gate_id": "sm-260526223122:telegram:5558998798:review",
        "action_id": "revise",
        "chat_id": "5558998798",
        "thread_id": "",
        "prompt_message_id": "457",
    }

    update = MagicMock()
    update.update_id = 4
    update.message = MagicMock()
    update.message.chat_id = 999999
    update.message.message_id = 889
    update.message.message_thread_id = 0
    update.message.chat.type = "private"
    update.message.from_user.id = 5558998798
    update.message.from_user.first_name = "Breanainn"
    update.message.text = "чужой origin"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_text_message(update, MagicMock())

    resolver.assert_not_called()
    assert "5558998798::" in adapter._canon_pending_revise


@pytest.mark.asyncio
async def test_dm_revise_followup_uses_pending_transport_thread_without_interruption():
    """DM revise follow-up is consumed before normal text handling and keeps Canon thread authority.

    pre: Revise button was clicked in a DM callback with embedded transport thread "1".
    post: the first DM text is recorded as revisionInstructions with thread_id="1" and is not enqueued.
    raises: AssertionError while DM revise text falls through as a normal interruption.
    """

    adapter = _make_adapter()
    adapter._enqueue_text_event = MagicMock()
    adapter._bot.send_message = AsyncMock()
    adapter._canon_pending_revise["5558998798::"] = {
        "run_id": "sm-260526223122",
        "gate_id": "sm-260526223122:telegram:5558998798:21676:review",
        "action_id": "revise",
        "chat_id": "5558998798",
        "thread_id": "21676",
        "prompt_message_id": "457",
    }

    update = MagicMock()
    update.update_id = 3
    update.message = MagicMock()
    update.message.chat_id = 5558998798
    update.message.message_id = 888
    update.message.message_thread_id = None
    update.message.chat.type = "private"
    update.message.from_user.id = 5558998798
    update.message.from_user.first_name = "Breanainn"
    update.message.text = "Сузить V1: убрать JSONL manifest и оставить только Markdown export."

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", return_value=_outcome()) as resolver:
        await adapter._handle_text_message(update, MagicMock())
        await _await_adapter_background_tasks(adapter)

    resolver.assert_called_once_with(
        gate_id="sm-260526223122:telegram:5558998798:21676:review",
        action_id="revise",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id="21676",
        message_id="457",
        revision_instructions="Сузить V1: убрать JSONL manifest и оставить только Markdown export.",
        sender=ANY,
    )
    sent_calls = adapter._bot.send_message.await_args_list
    assert len(sent_calls) == 1
    assert sent_calls[0].kwargs["message_thread_id"] == 21676
    assert "workflow продолжает работу отдельно" in sent_calls[0].kwargs["text"]
    adapter._enqueue_text_event.assert_not_called()
    assert "5558998798::" not in adapter._canon_pending_revise


@pytest.mark.asyncio
async def test_dm_revise_followup_failure_uses_pending_transport_thread_for_async_error_notice():
    """DM revise failure notice must keep pending transport-thread routing authority.

    pre: the captured revise follow-up arrived without a Telegram message_thread_id but the pending
         revise entry already stores the Canon transport thread.
    post: both the immediate ACK and the later async error notice use the stored transport thread
          instead of dropping back to the inbound follow-up thread.
    raises: AssertionError while async failure notices route outside the pending transport thread.
    """

    adapter = _make_adapter()
    adapter._enqueue_text_event = MagicMock()
    adapter._bot.send_message = AsyncMock()
    adapter._canon_pending_revise["5558998798::"] = {
        "run_id": "sm-260526223122",
        "gate_id": "sm-260526223122:telegram:5558998798:21676:review",
        "action_id": "revise",
        "chat_id": "5558998798",
        "thread_id": "21676",
        "prompt_message_id": "457",
    }

    update = MagicMock()
    update.update_id = 4
    update.message = MagicMock()
    update.message.chat_id = 5558998798
    update.message.message_id = 889
    update.message.message_thread_id = None
    update.message.chat.type = "private"
    update.message.from_user.id = 5558998798
    update.message.from_user.first_name = "Breanainn"
    update.message.text = "Уточнить acceptance criteria."

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review_outcome", side_effect=RuntimeError("boom")):
        await adapter._handle_text_message(update, MagicMock())
        await _await_adapter_background_tasks(adapter)

    sent_calls = adapter._bot.send_message.await_args_list
    assert len(sent_calls) == 2
    assert sent_calls[0].kwargs["message_thread_id"] == 21676
    assert "workflow продолжает работу отдельно" in sent_calls[0].kwargs["text"]
    assert sent_calls[1].kwargs["message_thread_id"] == 21676
    assert "не записался" in sent_calls[1].kwargs["text"]
    adapter._enqueue_text_event.assert_not_called()
    assert "5558998798::" not in adapter._canon_pending_revise
