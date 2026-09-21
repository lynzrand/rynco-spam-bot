"""Behavioral tests with fake Telegram; no network or credentials required."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api import APIError, Classifier
from bot import Bot, database, identity

CHAT = -100123
USER = {"id": 42, "first_name": "Example"}


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.members = {}
        self.failures = {}
        self.sequence = 1000
        self.photos = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in self.failures:
            raise self.failures[method]
        if method == "getChatMember":
            return self.members.get(params["user_id"], {"status": "member"})
        if method == "getChat":
            return {"description": "Discussion", "type": "supergroup"}
        if method == "getUserProfilePhotos":
            return {"photos": self.photos}
        if method == "sendMessage":
            self.sequence += 1
            return {"message_id": self.sequence}
        return True

    def image(self, file_id):
        if "image" in self.failures:
            raise self.failures["image"]
        return "data:image/png;base64," + file_id

    def calls_for(self, method):
        return [params for name, params in self.calls if name == method]


class FakeClassifier:
    def __init__(self):
        self.verdict = "clean"
        self.calls = []
        self.error = None

    def classify(self, evidence, images):
        self.calls.append((evidence, images))
        if self.error:
            raise self.error
        return {"verdict": self.verdict, "reason": "Test evidence."}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.db = database(self.path)
        self.tg, self.model = FakeTelegram(), FakeClassifier()
        self.bot = Bot(self.db, self.tg, self.model, {CHAT})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def message(self, number=1, **extra):
        return {
            "message_id": number,
            "date": int(time.time()),
            "chat": {"id": CHAT, "type": "supergroup"},
            "from": USER,
            "text": "Hello",
            **extra,
        }

    def send(self, number=1, **extra):
        message = self.message(number, **extra)
        self.bot.update({"update_id": number, "message": message})
        self.bot.maintain()
        return message

    def case(self):
        return self.db.execute(
            "SELECT * FROM cases ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def vote(self, voter, choice, **message_overrides):
        case = self.case()
        message = {
            "chat": {"id": CHAT},
            "message_id": case["ban_message" if choice == "undo" else "review_message"],
            **message_overrides,
        }
        self.bot.vote(
            {
                "id": f"callback-{voter}",
                "from": {"id": voter},
                "message": message,
                "data": f"case:{case['id']}:{choice}",
            }
        )
        self.bot.maintain()

    def test_first_ten_deduplicate_and_recheck_edits_after_restart(self):
        first = self.send()
        self.bot.update({"message": first})
        for number in range(2, 12):
            self.send(number)
        self.assertEqual(len(self.model.calls), 10)
        self.db.close()
        self.db = database(self.path)
        self.bot = Bot(self.db, self.tg, self.model, {CHAT})
        self.send(12)
        self.assertEqual(len(self.model.calls), 10)
        self.bot.update({"edited_message": {**first, "text": "Edited solicitation"}})
        self.assertEqual(len(self.model.calls), 11)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 12
        )

    def test_channel_identity_overrides_fake_user_and_counts_separately(self):
        self.model.verdict = "spam"
        self.send(
            sender_chat={"id": -900, "title": "A channel"},
            **{"from": {"id": 1087968824, "is_bot": True}},
        )
        self.assertEqual(
            self.tg.calls_for("banChatSenderChat"),
            [{"chat_id": CHAT, "sender_chat_id": -900}],
        )
        self.assertFalse(self.tg.calls_for("banChatMember"))
        self.assertIn(
            {"chat_id": CHAT, "message_id": 1}, self.tg.calls_for("deleteMessage")
        )

    def test_anonymous_admin_and_automatic_forward_are_skipped(self):
        self.send(sender_chat={"id": CHAT, "title": "Group"})
        self.send(2, sender_chat={"id": -900}, is_automatic_forward=True)
        self.tg.members[42] = {"status": "administrator"}
        self.send(3)
        self.assertFalse(self.model.calls)
        self.assertIsNone(identity(self.message(is_automatic_forward=True)))

    def test_commenter_need_not_be_a_member(self):
        self.tg.members[42] = {"status": "left"}
        self.send(reply_to_message={"message_id": 99, "is_automatic_forward": True})
        self.assertEqual(len(self.model.calls), 1)

    def test_join_targets_new_member_not_inviter_and_does_not_consume_budget(self):
        invited = {"id": 84, "first_name": "Joined"}
        self.send(new_chat_members=[invited])
        self.bot.update(
            {
                "chat_member": {
                    "chat": {"id": CHAT},
                    "old_chat_member": {"status": "left"},
                    "new_chat_member": {"status": "member", "user": invited},
                }
            }
        )
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0][0]["event"], "join")
        for number in range(2, 12):
            self.send(number, **{"from": invited})
        self.assertEqual(len(self.model.calls), 11)
        self.assertEqual(
            self.db.execute("SELECT identity FROM subjects").fetchone()[0], "user:84"
        )

    def test_member_update_then_join_message_preserves_service_message_for_deletion(
        self,
    ):
        self.bot.update(
            {
                "chat_member": {
                    "chat": {"id": CHAT},
                    "old_chat_member": {"status": "left"},
                    "new_chat_member": {"status": "member", "user": USER},
                }
            }
        )
        self.send(new_chat_members=[USER])
        self.model.verdict = "spam"
        self.send(2)
        deleted = self.tg.calls_for("deleteMessage")
        self.assertIn({"chat_id": CHAT, "message_id": 1}, deleted)
        self.assertIn({"chat_id": CHAT, "message_id": 2}, deleted)

    def test_third_unique_vote_wins_and_later_votes_cannot_overturn(self):
        self.model.verdict = "suspicious"
        self.send()
        self.vote(1, "spam")
        self.vote(1, "spam")
        self.vote(1, "clean")
        self.vote(2, "clean")
        self.vote(3, "spam")
        self.assertEqual(self.case()["phase"], "review")
        self.vote(4, "spam")
        self.assertEqual(self.case()["phase"], "banned")
        self.vote(5, "clean")
        self.assertEqual(self.case()["phase"], "banned")
        self.assertEqual(self.db.execute("SELECT count(*) FROM votes").fetchone()[0], 4)
        self.assertTrue(self.tg.calls_for("banChatMember")[0]["revoke_messages"])

    def test_clean_wins_without_ban_but_does_not_exempt_future_spam(self):
        self.model.verdict = "suspicious"
        self.send()
        for voter in (1, 2, 3):
            self.vote(voter, "clean")
        self.assertEqual(self.case()["phase"], "clean")
        self.assertFalse(self.tg.calls_for("banChatMember"))
        self.model.verdict = "spam"
        self.send(2)
        self.assertEqual(self.case()["phase"], "banned")

    def test_self_nonmember_and_wrong_message_votes_are_rejected(self):
        self.model.verdict = "suspicious"
        self.send()
        self.vote(42, "spam")
        self.tg.members[5] = {"status": "left"}
        self.vote(5, "spam")
        self.vote(6, "spam", message_id=99999)
        self.vote(7, "spam", chat={"id": -1})
        self.assertEqual(self.db.execute("SELECT count(*) FROM votes").fetchone()[0], 0)

    def test_undo_requires_moderator_and_persistently_exempts(self):
        self.model.verdict = "spam"
        self.send()
        self.vote(1, "undo")
        self.assertEqual(self.case()["phase"], "banned")
        self.tg.members[1] = {"status": "administrator", "can_restrict_members": True}
        self.vote(1, "undo")
        self.assertEqual(self.case()["phase"], "restored")
        self.assertTrue(self.tg.calls_for("unbanChatMember")[0]["only_if_banned"])
        self.db.close()
        self.db = database(self.path)
        self.bot = Bot(self.db, self.tg, self.model, {CHAT})
        self.send(2)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.tg.calls_for("banChatMember")), 1)

    def test_channel_undo_uses_sender_chat_api(self):
        self.model.verdict = "spam"
        self.send(sender_chat={"id": -900, "title": "Channel"})
        self.tg.members[1] = {"status": "creator"}
        self.vote(1, "undo")
        self.assertEqual(
            self.tg.calls_for("unbanChatSenderChat"),
            [{"chat_id": CHAT, "sender_chat_id": -900}],
        )

    def test_expired_notice_is_deleted_after_restart_and_rejects_stale_undo(self):
        self.model.verdict = "spam"
        self.send()
        notice = self.case()["ban_message"]
        self.assertAlmostEqual(self.case()["expires"], time.time() + 1800, delta=2)
        with self.db:
            self.db.execute("UPDATE cases SET expires=?", (time.time() - 1,))
        self.db.close()
        self.db = database(self.path)
        self.bot = Bot(self.db, self.tg, self.model, {CHAT})
        self.bot.maintain()
        self.assertIn(
            {"chat_id": CHAT, "message_id": notice}, self.tg.calls_for("deleteMessage")
        )
        self.tg.members[1] = {"status": "creator"}
        self.vote(1, "undo")
        self.assertEqual(self.case()["phase"], "banned")
        self.assertFalse(self.tg.calls_for("unbanChatMember"))

    def test_failed_ban_is_retried_without_claiming_success(self):
        self.model.verdict = "spam"
        self.tg.failures["banChatMember"] = APIError("Telegram", 403)
        self.send()
        self.assertEqual(self.case()["phase"], "ban")
        self.assertIsNone(self.case()["ban_message"])
        del self.tg.failures["banChatMember"]
        with self.db:
            self.db.execute("UPDATE cases SET retry_at=0")
        self.bot.maintain()
        self.assertEqual(self.case()["phase"], "banned")

    def test_failed_unban_is_not_exempt_until_success(self):
        self.model.verdict = "spam"
        self.send()
        self.tg.members[1] = {"status": "creator"}
        self.tg.failures["unbanChatMember"] = APIError("Telegram", 429, 20)
        self.vote(1, "undo")
        self.assertEqual(self.case()["phase"], "restore")
        self.assertEqual(
            self.db.execute("SELECT exempt FROM subjects").fetchone()[0], 0
        )
        del self.tg.failures["unbanChatMember"]
        with self.db:
            self.db.execute("UPDATE cases SET retry_at=0")
        self.bot.maintain()
        self.assertEqual(
            self.db.execute("SELECT exempt FROM subjects").fetchone()[0], 1
        )

    def test_missing_review_notice_does_not_prevent_ban_appeal(self):
        # A moderator may delete the review while the third vote is in flight.
        self.model.verdict = "suspicious"
        self.send()
        self.tg.failures["editMessageText"] = APIError(
            "Telegram", 400, description="Bad Request: message to edit not found"
        )
        for voter in (1, 2, 3):
            self.vote(voter, "spam")
        self.assertEqual(self.case()["phase"], "banned")
        self.assertIsNotNone(self.case()["ban_message"])
        self.assertEqual(self.case()["dirty"], 0)

    def test_history_deletion_failure_does_not_block_notice_expiry(self):
        # A failed old-message deletion must not leave the appeal notice forever.
        self.model.verdict = "spam"
        self.send()
        notice = self.case()["ban_message"]
        with self.db:
            self.db.execute("UPDATE cases SET expires=?,deleted=0", (time.time() - 1,))
        original = self.tg.call

        def call(method, **params):
            if method == "deleteMessage" and params["message_id"] == 1:
                raise APIError("Telegram", 400)
            return original(method, **params)

        self.tg.call = call
        self.bot.maintain()
        self.assertIn(
            {"chat_id": CHAT, "message_id": notice}, self.tg.calls_for("deleteMessage")
        )
        self.assertIsNone(self.case()["expires"])

    def test_profile_lookup_failure_does_not_imply_empty_profile(self):
        self.tg.failures["getChat"] = APIError("Telegram", 400)
        self.tg.failures["getUserProfilePhotos"] = APIError("Telegram", 400)
        self.send()
        evidence = self.model.calls[0][0]
        self.assertIsNone(evidence["profile"]["description"])
        self.assertEqual(evidence["profile"]["photo_status"], "unknown")
        self.assertIsNone(self.case())

    def test_admin_promotion_before_vote_prevents_ban(self):
        self.model.verdict = "suspicious"
        self.send()
        self.tg.members[42] = {"status": "administrator"}
        for voter in (1, 2, 3):
            self.vote(voter, "spam")
        self.assertEqual(self.case()["phase"], "clean")
        self.assertFalse(self.tg.calls_for("banChatMember"))

    def test_classifier_failure_goes_to_review(self):
        self.model.error = APIError("Classifier", 503)
        self.send()
        self.assertEqual(self.case()["phase"], "review")
        self.assertFalse(self.tg.calls_for("banChatMember"))

    def test_unreadable_media_cannot_silently_pass(self):
        self.tg.failures["image"] = APIError("Image")
        self.send(photo=[{"file_id": "bad"}])
        self.assertEqual(self.case()["phase"], "review")

    def test_profile_and_message_images_are_labelled(self):
        self.tg.photos = [[{"file_id": "avatar"}]]
        self.send(photo=[{"file_id": "message"}], caption="Caption")
        evidence, images = self.model.calls[0]
        self.assertEqual(evidence["profile"]["photo_status"], "visible")
        self.assertEqual(
            [label for label, _ in images], ["Sender profile photo", "Message image"]
        )
        self.assertEqual(evidence["message"]["caption"], "Caption")

    def test_inbox_survives_restart_and_duplicate_delivery(self):
        update = {"update_id": 10, "message": self.message()}
        self.bot.receive([update, update])
        self.assertEqual(
            self.db.execute("SELECT value FROM settings").fetchone()[0], 11
        )
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)
        self.db.close()
        self.db = database(self.path)
        self.bot = Bot(self.db, self.tg, self.model, {CHAT})
        self.bot.drain()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

    def test_foreign_chat_is_ignored(self):
        self.send(chat={"id": -999, "type": "supergroup"})
        self.assertFalse(self.model.calls)


class ClassifierTests(unittest.TestCase):
    def test_multimodal_request_and_strict_response_validation(self):
        client = Classifier(
            "https://example.invalid/v1", "test-key", "test-model", "Discussion"
        )
        completion = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"verdict": "clean", "reason": "Relevant discussion."}
                        )
                    },
                }
            ]
        }
        with patch("api.request_json", return_value=completion) as request:
            self.assertEqual(
                client.classify(
                    {"message": "Hello"}, [("avatar", "data:image/png;base64,test")]
                )["verdict"],
                "clean",
            )
            payload = request.call_args.args[1]
            self.assertEqual(payload["model"], "test-model")
            self.assertEqual(
                payload["messages"][-1]["content"][-1]["image_url"]["url"],
                "data:image/png;base64,test",
            )
            completion["choices"][0]["finish_reason"] = "length"
            with self.assertRaises(APIError):
                client.classify({}, [])

    def test_invalid_verdict_never_authorizes_ban(self):
        client = Classifier("https://example.invalid/v1", "test", "test", "test")
        for content in (
            '{"verdict":"ban","reason":"x"}',
            "[]",
            "not json",
            '{"verdict":"spam","reason":123}',
        ):
            with self.subTest(content=content), patch(
                "api.request_json",
                return_value={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": content}}
                    ]
                },
            ):
                with self.assertRaises(APIError):
                    client.classify({}, [])


if __name__ == "__main__":
    unittest.main()
