from typing import Optional, List

import time

import asyncio
import datetime
import uuid

import pytest
from aioresponses import aioresponses

from unittest.mock import patch

from rasa.core import jobs
from rasa.core.actions.action import ACTION_LISTEN_NAME, ACTION_SESSION_START_NAME
from rasa.core.agent import Agent
from rasa.core.channels.channel import CollectingOutputChannel, UserMessage
from rasa.core.events import (
    ActionExecuted,
    BotUttered,
    ReminderCancelled,
    ReminderScheduled,
    Restarted,
    UserUttered,
    SessionStarted,
    Event,
    FollowupAction,
)
from rasa.core.trackers import DialogueStateTracker
from rasa.core.slots import Slot
from rasa.core.interpreter import RasaNLUHttpInterpreter
from rasa.core.processor import MessageProcessor
from rasa.utils.endpoints import EndpointConfig
from tests.utilities import latest_request


import logging

logger = logging.getLogger(__name__)


async def test_message_processor(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    await default_processor.handle_message(
        UserMessage('/greet{"name":"Core"}', default_channel)
    )
    assert {
        "recipient_id": "default",
        "text": "hey there Core!",
    } == default_channel.latest_output()


async def test_message_id_logging(default_processor: MessageProcessor):
    from rasa.core.trackers import DialogueStateTracker

    message = UserMessage("If Meg was an egg would she still have a leg?")
    tracker = DialogueStateTracker("1", [])
    await default_processor._handle_message_with_tracker(message, tracker)
    logged_event = tracker.events[-1]

    assert logged_event.message_id == message.message_id
    assert logged_event.message_id is not None


async def test_parsing(default_processor: MessageProcessor):
    message = UserMessage('/greet{"name": "boy"}')
    parsed = await default_processor._parse_message(message)
    assert parsed["intent"]["name"] == "greet"
    assert parsed["entities"][0]["entity"] == "name"


async def test_log_unseen_feature(default_processor: MessageProcessor):
    message = UserMessage('/dislike{"test_entity": "RASA"}')
    parsed = await default_processor._parse_message(message)
    with pytest.warns(UserWarning) as record:
        default_processor._log_unseen_features(parsed)
    assert len(record) == 2
    assert (
        record[0].message.args[0]
        == "Interpreter parsed an intent 'dislike' that is not defined in the domain."
    )
    assert (
        record[1].message.args[0]
        == "Interpreter parsed an entity 'test_entity' that is not defined in the domain."
    )


async def test_default_intent_recognized(default_processor: MessageProcessor):
    message = UserMessage("/restart")
    parsed = await default_processor._parse_message(message)
    with pytest.warns(None) as record:
        default_processor._log_unseen_features(parsed)
    assert len(record) == 0


async def test_http_parsing():
    message = UserMessage("lunch?")

    endpoint = EndpointConfig("https://interpreter.com")
    with aioresponses() as mocked:
        mocked.post("https://interpreter.com/model/parse", repeat=True, status=200)

        inter = RasaNLUHttpInterpreter(endpoint=endpoint)
        try:
            await MessageProcessor(inter, None, None, None, None)._parse_message(
                message
            )
        except KeyError:
            pass  # logger looks for intent and entities, so we except

        r = latest_request(mocked, "POST", "https://interpreter.com/model/parse")

        assert r


async def mocked_parse(self, text, message_id=None, tracker=None):
    """Mock parsing a text message and augment it with the slot
    value from the tracker's state."""

    return {
        "intent": {"name": "", "confidence": 0.0},
        "entities": [],
        "text": text,
        "requested_language": tracker.get_slot("requested_language"),
    }


async def test_parsing_with_tracker():
    tracker = DialogueStateTracker.from_dict("1", [], [Slot("requested_language")])

    # we'll expect this value 'en' to be part of the result from the interpreter
    tracker._set_slot("requested_language", "en")

    endpoint = EndpointConfig("https://interpreter.com")
    with aioresponses() as mocked:
        mocked.post("https://interpreter.com/parse", repeat=True, status=200)

        # mock the parse function with the one defined for this test
        with patch.object(RasaNLUHttpInterpreter, "parse", mocked_parse):
            interpreter = RasaNLUHttpInterpreter(endpoint=endpoint)
            agent = Agent(None, None, interpreter)
            result = await agent.parse_message_using_nlu_interpreter("lunch?", tracker)

            assert result["requested_language"] == "en"


async def test_reminder_scheduled(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex

    reminder = ReminderScheduled("utter_greet", datetime.datetime.now())
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    tracker.update(UserUttered("test"))
    tracker.update(ActionExecuted("action_reminder_reminder"))
    tracker.update(reminder)

    default_processor.tracker_store.save(tracker)

    await default_processor.handle_reminder(
        reminder, sender_id, default_channel, default_processor.nlg
    )

    # retrieve the updated tracker
    t = default_processor.tracker_store.retrieve(sender_id)

    assert t.events[-4] == UserUttered(None)
    assert t.events[-3] == ActionExecuted("utter_greet")
    assert t.events[-2] == BotUttered(
        "hey there None!",
        {
            "elements": None,
            "buttons": None,
            "quick_replies": None,
            "attachment": None,
            "image": None,
            "custom": None,
        },
    )
    assert t.events[-1] == ActionExecuted("action_listen")


async def test_reminder_aborted(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex

    reminder = ReminderScheduled(
        "utter_greet", datetime.datetime.now(), kill_on_user_message=True
    )
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    tracker.update(reminder)
    tracker.update(UserUttered("test"))  # cancels the reminder

    default_processor.tracker_store.save(tracker)
    await default_processor.handle_reminder(
        reminder, sender_id, default_channel, default_processor.nlg
    )

    # retrieve the updated tracker
    t = default_processor.tracker_store.retrieve(sender_id)
    assert len(t.events) == 4  # nothing should have been executed


async def test_reminder_cancelled(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    trackers = []
    for sender_id in sender_ids:
        tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

        tracker.update(UserUttered("test"))
        tracker.update(ActionExecuted("action_reminder_reminder"))
        tracker.update(
            ReminderScheduled(
                "utter_greet", datetime.datetime.now(), kill_on_user_message=True
            )
        )
        trackers.append(tracker)

    # cancel reminder for the first user
    trackers[0].update(ReminderCancelled("utter_greet"))

    for tracker in trackers:
        default_processor.tracker_store.save(tracker)
        await default_processor._schedule_reminders(
            tracker.events, tracker, default_channel, default_processor.nlg
        )
    # check that the jobs were added
    assert len((await jobs.scheduler()).get_jobs()) == 2

    for tracker in trackers:
        await default_processor._cancel_reminders(tracker.events, tracker)
    # check that only one job was removed
    assert len((await jobs.scheduler()).get_jobs()) == 1

    # execute the jobs
    await asyncio.sleep(5)

    tracker_0 = default_processor.tracker_store.retrieve(sender_ids[0])
    # there should be no utter_greet action
    assert ActionExecuted("utter_greet") not in tracker_0.events

    tracker_1 = default_processor.tracker_store.retrieve(sender_ids[1])
    # there should be utter_greet action
    assert ActionExecuted("utter_greet") in tracker_1.events


async def test_reminder_restart(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex

    reminder = ReminderScheduled(
        "utter_greet", datetime.datetime.now(), kill_on_user_message=False
    )
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    tracker.update(reminder)
    tracker.update(Restarted())  # cancels the reminder
    tracker.update(UserUttered("test"))

    default_processor.tracker_store.save(tracker)
    await default_processor.handle_reminder(
        reminder, sender_id, default_channel, default_processor.nlg
    )

    # retrieve the updated tracker
    t = default_processor.tracker_store.retrieve(sender_id)
    assert len(t.events) == 5  # nothing should have been executed


@pytest.mark.parametrize(
    "events_to_apply,is_legacy",
    [
        # just an action listen means it's legacy
        ([ActionExecuted(action_name=ACTION_LISTEN_NAME)], True),
        # action listen and session at the beginning start means it isn't legacy
        ([SessionStarted(), ActionExecuted(action_name=ACTION_LISTEN_NAME)], False),
        # just a single event means it's legacy
        ([UserUttered("hello")], True),
    ],
)
async def test_is_legacy_tracker(
    events_to_apply: List[Event], is_legacy: bool, default_processor: MessageProcessor,
):
    sender_id = uuid.uuid4().hex

    # create a new tracker without events
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)
    tracker.events.clear()

    for event in events_to_apply:
        tracker.update(event)

    # noinspection PyProtectedMember
    assert default_processor._is_legacy_tracker(tracker) == is_legacy


@pytest.mark.parametrize(
    "event_to_apply,session_length_in_minutes,has_expired",
    [
        # session start is way in the past
        (SessionStarted(timestamp=1), 60, True),
        # session start is very recent
        (SessionStarted(timestamp=time.time()), 1, False),
        # there is no session start event (legacy tracker)
        (UserUttered("hello", timestamp=time.time()), 1, False),
        # there is no event
        (None, 1, False),
    ],
)
async def test_has_session_expired(
    event_to_apply: Optional[Event],
    session_length_in_minutes: int,
    has_expired: bool,
    default_processor: MessageProcessor,
):
    sender_id = uuid.uuid4().hex

    # create new tracker without events
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)
    tracker.events.clear()

    # apply desired event
    if event_to_apply:
        tracker.update(event_to_apply)

    # noinspection PyProtectedMember
    assert (
        default_processor._has_session_expired(
            tracker, session_length_in_minutes=session_length_in_minutes
        )
        == has_expired
    )


# noinspection PyProtectedMember
async def test_update_tracker_session(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor,
):
    sender_id = uuid.uuid4().hex
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    # make sure session expires and run tracker session update
    await asyncio.sleep(1e-2)  # in seconds
    await default_processor._update_tracker_session(tracker, default_channel, 1e-5)

    # the save is not called in _update_tracker_session()
    default_processor._save_tracker(tracker)

    # inspect tracker and make sure all events are present
    tracker = default_processor.tracker_store.retrieve(sender_id)
    assert list(tracker.events) == [
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        FollowupAction(ACTION_LISTEN_NAME),
    ]
