"""Behavior tests for Canon Telegram review callback semantics."""

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.telegram import TelegramAdapter


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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
        await adapter._handle_callback_query(update, MagicMock())

    assert resolver.call_count == 0
    assert adapter._canon_pending_revise["12345::999"]["run_id"] == "cg-redprep-1"


@pytest.mark.asyncio
async def test_revise_followup_same_origin_records_revision_text():
    """AC-S3-003: next authorized same-origin text must record revise with payload text.

    pre: revise callback armed pending state for one chat/thread.
    post: first authorized text message from same origin resolves Canon review with
          revision instructions and clears pending state.
    raises: AssertionError while follow-up text is ignored or missing from payload.
    """

    adapter = _make_adapter()
    adapter._canon_pending_revise["12345::999"] = {
        "run_id": "cg-redprep-2",
        "chat_id": "12345",
        "thread_id": "999",
        "prompt_message_id": "777",
    }

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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
        await adapter._handle_text_message(update, MagicMock())

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
    assert "12345::999" not in adapter._canon_pending_revise


@pytest.mark.asyncio
async def test_revise_followup_sends_progress_before_slow_resolver():
    """Revise text must get immediate chat feedback before slow Canon resume work starts."""

    adapter = _make_adapter()
    call_order = []
    adapter._canon_pending_revise["12345::999"] = {
        "run_id": "cg-redprep-progress",
        "chat_id": "12345",
        "thread_id": "999",
        "prompt_message_id": "777",
    }

    update = MagicMock()
    update.update_id = 10
    update.message = MagicMock()
    update.message.chat_id = 12345
    update.message.message_id = 888
    update.message.message_thread_id = 999
    update.message.chat.type = "supergroup"
    update.message.from_user.id = 333
    update.message.from_user.first_name = "Operator"
    update.message.text = "Не pong, а pang"

    async def _send_message_side_effect(**kwargs):
        call_order.append(("send", kwargs["text"]))

    adapter._bot.send_message = AsyncMock(side_effect=_send_message_side_effect)

    def _resolver_side_effect(**kwargs):
        call_order.append(("resolver", kwargs["revision_instructions"]))
        return "recorded"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", side_effect=_resolver_side_effect):
        await adapter._handle_text_message(update, MagicMock())

    assert call_order[0][0] == "send"
    assert "Canon" in call_order[0][1]
    assert call_order[1] == ("resolver", "Не pong, а pang")
    assert call_order[-1] == ("send", "recorded")


@pytest.mark.asyncio
async def test_revise_followup_wrong_origin_is_ignored_fail_closed():
    """AC-S3-003: wrong-origin follow-up must not satisfy pending revise capture.

    pre: pending revise state exists for chat/thread A.
    post: message from chat/thread B does not call resolver and pending state remains.
    raises: AssertionError while wrong-origin text can resolve revise.
    """

    adapter = _make_adapter()
    adapter._canon_pending_revise["12345::999"] = {
        "run_id": "cg-redprep-3",
        "chat_id": "12345",
        "thread_id": "999",
        "prompt_message_id": "777",
    }

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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
        await adapter._handle_text_message(update, MagicMock())

    resolver.assert_not_called()
    assert "12345::999" in adapter._canon_pending_revise


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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
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
        return "recorded"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", side_effect=_resolver_side_effect):
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
        return "recorded"

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", side_effect=_resolver_side_effect):
        await adapter._handle_callback_query(update, MagicMock())

    assert call_order[0][0] == "edit"
    assert "Выбор зафиксирован" in call_order[0][1]
    assert "✅ Да" in call_order[0][1]
    assert "Обрабатываю" in call_order[0][1]
    assert call_order[0][2] is None
    assert call_order[1] == ("resolver", "approve")


@pytest.mark.asyncio
async def test_completed_canon_callback_sends_operator_closeout_as_new_message():
    """Completed Canon reviews must produce an operator-visible chat message, not only edit the card.

    pre: approve callback resolves to a completed current-gateway closeout with artifact refs.
    post: Telegram adapter edits the card and also sends the closeout as a fresh message to
          the same chat/thread so completion is visible in the conversation flow.
    raises: AssertionError while closeout is only hidden in an edited review card.
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
        "Артефакты:\n"
        "- `artifacts.solution-modeling.spec` -> `/tmp/spec.json`"
    )

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value=closeout_text):
        await adapter._handle_callback_query(update, MagicMock())

    assert query.edit_message_text.call_count == 2
    first_edit = query.edit_message_text.call_args_list[0].kwargs
    assert first_edit["reply_markup"] is None
    assert "Выбор зафиксирован: ✅ Да" in first_edit["text"]
    assert "Статус: ⏳ Обрабатываю" in first_edit["text"]

    final_edit = query.edit_message_text.call_args_list[-1].kwargs
    assert final_edit["reply_markup"] is None
    assert "Статус: ✅ Завершено" in final_edit["text"]
    assert closeout_text in final_edit["text"]

    adapter._bot.send_message.assert_called_once()
    sent = adapter._bot.send_message.call_args.kwargs
    assert sent["chat_id"] == 5558998798
    assert sent["text"] == closeout_text


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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
        await adapter._handle_text_message(update2, MagicMock())

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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
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
    adapter._canon_pending_revise["5558998798::"] = {
        "run_id": "sm-260526223122",
        "gate_id": "sm-260526223122:telegram:5558998798:1:review",
        "action_id": "revise",
        "chat_id": "5558998798",
        "thread_id": "1",
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

    with patch("tools.canon_gateway_review.resolve_telegram_canon_review", return_value="recorded") as resolver:
        await adapter._handle_text_message(update, MagicMock())

    resolver.assert_called_once_with(
        gate_id="sm-260526223122:telegram:5558998798:1:review",
        action_id="revise",
        actor_id="5558998798",
        actor_name="Breanainn",
        chat_id="5558998798",
        thread_id="1",
        message_id="457",
        revision_instructions="Сузить V1: убрать JSONL manifest и оставить только Markdown export.",
        sender=ANY,
    )
    adapter._enqueue_text_event.assert_not_called()
    assert "5558998798::" not in adapter._canon_pending_revise
