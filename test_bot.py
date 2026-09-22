"""Behavioral tests with fake Telegram; no network or credentials required."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api import APIError, Classifier
from bot import UNDO_WINDOW, Bot, database, identity

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
        self.reason = "Test evidence."
        self.basis = None
        self.calls = []
        self.error = None

    def classify(self, evidence, images):
        self.calls.append((evidence, images))
        if self.error:
            raise self.error
        result = {"verdict": self.verdict, "reason": self.reason}
        if self.basis:
            result["basis"] = self.basis
        return result


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

    def test_first_ten_deduplicate_and_skip_edits_after_restart(self):
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
        self.assertEqual(len(self.model.calls), 10)
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 12
        )

    def test_edit_at_nine_is_checked_but_edit_at_ten_is_not(self):
        for number in range(1, 10):
            self.send(number)
        self.bot.update({"edited_message": self.message(100, text="Changed")})
        self.assertEqual(len(self.model.calls), 10)
        self.send(10)
        self.bot.update({"edited_message": self.message(100, text="Changed again")})
        self.assertEqual(len(self.model.calls), 11)
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 10
        )

    def test_original_retried_after_edit_still_counts_once(self):
        # A transient API failure can defer an original update until after its edit.
        self.bot.update({"edited_message": self.message(1, text="Edited")})
        original = self.message(1)
        self.bot.update({"message": original})
        self.bot.update({"message": original})
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 1
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

    def test_old_unseen_edits_are_checked_without_consuming_budget(self):
        # Edits arriving for messages predating the bot must not use a new-message slot.
        for number in range(20):
            self.bot.update({"edited_message": self.message(100 + number)})
        self.assertEqual(len(self.model.calls), 20)
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 0
        )
        for number in range(1, 12):
            self.send(number)
        self.assertEqual(len(self.model.calls), 30)
        self.model.verdict = "spam"
        self.bot.update({"edited_message": self.message(11, text="New advertising")})
        self.bot.maintain()
        self.assertIsNone(self.case())
        self.assertEqual(len(self.model.calls), 30)
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 11
        )

    def reaction(self, update_id=100, user=USER, actor_chat=None, old=None, new=None):
        return {
            "update_id": update_id,
            "message_reaction": {
                "chat": {"id": CHAT},
                "message_id": 900,
                "date": int(time.time()),
                "user": user,
                "actor_chat": actor_chat,
                "old_reaction": old or [],
                "new_reaction": (
                    new if new is not None else [{"type": "emoji", "emoji": "👍"}]
                ),
            },
        }

    def test_untrusted_reactions_check_reactor_without_deleting_target_message(self):
        self.model.verdict = "spam"
        self.bot.update(self.reaction())
        self.bot.maintain()
        self.assertEqual(self.model.calls[0][0]["event"], "reaction")
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 0
        )
        # Profile-only evidence now requires member confirmation before a ban.
        self.assertEqual(self.case()["phase"], "review")
        self.assertFalse(self.tg.calls_for("banChatMember"))
        for voter in (1, 2, 3):
            self.vote(voter, "spam")
        self.assertEqual(self.case()["phase"], "banned")
        self.assertFalse(self.tg.calls_for("deleteMessage"))
        self.assertEqual(
            self.tg.calls_for("deleteAllMessageReactions"),
            [{"chat_id": CHAT, "user_id": 42}],
        )

    def test_reactions_do_not_create_trust_or_consume_message_budget(self):
        update = self.reaction()
        self.bot.update(update)
        self.bot.update(update)
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(self.bot.trusted(CHAT, "user:42"))
        for number in range(1, 11):
            self.send(number)
        self.assertTrue(self.bot.trusted(CHAT, "user:42"))
        self.bot.update(self.reaction(101))
        self.assertEqual(len(self.model.calls), 11)

    def test_reactions_to_own_review_and_ban_notices_are_ignored(self):
        # Regression: reacting to our review notice used to ban an innocent member.
        for verdict, field in (
            ("suspicious", "review_message"),
            ("spam", "ban_message"),
        ):
            with self.subTest(notice=field):
                self.model.verdict = verdict
                self.send(1 if verdict == "suspicious" else 2)
                notice = self.case()[field]
                before = self.db.execute("SELECT count(*) FROM cases").fetchone()[0]
                observations = self.db.execute(
                    "SELECT count(*) FROM observations"
                ).fetchone()[0]
                self.tg.calls.clear()
                self.model.calls.clear()
                self.model.verdict = "spam"
                update = self.reaction(user={"id": 99, "first_name": "Reactor"})
                update["message_reaction"]["message_id"] = notice
                self.bot.update(update)
                self.bot.maintain()
                self.assertFalse(self.model.calls)
                self.assertEqual(
                    self.db.execute("SELECT count(*) FROM cases").fetchone()[0], before
                )
                self.assertEqual(
                    self.db.execute("SELECT count(*) FROM observations").fetchone()[0],
                    observations,
                )
                self.assertFalse(self.tg.calls_for("banChatMember"))
                self.assertFalse(self.tg.calls_for("deleteMessage"))
                self.assertIsNone(
                    self.db.execute(
                        "SELECT 1 FROM subjects WHERE identity='user:99'"
                    ).fetchone()
                )

    def test_profile_only_reaction_spam_requires_review(self):
        self.model.verdict = "spam"
        self.bot.update(self.reaction())
        self.bot.maintain()
        self.assertEqual(self.case()["phase"], "review")
        self.assertIn("Profile-only evidence", self.case()["reason"])
        self.assertFalse(self.tg.calls_for("banChatMember"))
        self.assertFalse(self.tg.calls_for("deleteMessage"))

    def test_profile_only_join_spam_requires_review(self):
        self.model.verdict = "spam"
        self.send(new_chat_members=[USER])
        self.assertEqual(self.case()["phase"], "review")
        self.assertFalse(self.tg.calls_for("banChatMember"))
        self.assertFalse(self.tg.calls_for("deleteMessage"))

    def test_message_and_edit_spam_still_ban(self):
        self.model.verdict = "spam"
        self.send()
        self.assertEqual(self.case()["phase"], "banned")
        self.assertEqual(self.tg.calls_for("banChatMember")[0]["user_id"], 42)
        self.bot.update({"edited_message": self.message(2, **{"from": {"id": 99}})})
        self.bot.maintain()
        self.assertEqual(self.case()["phase"], "banned")
        self.assertEqual(self.tg.calls_for("banChatMember")[-1]["user_id"], 99)

    def test_three_member_ban_notice_votes_restore_and_exempt(self):
        self.model.verdict = "spam"
        self.send()
        notice = self.case()["ban_message"]
        buttons = self.tg.calls_for("sendMessage")[-1]["reply_markup"][
            "inline_keyboard"
        ][0]
        self.assertEqual([b["text"] for b in buttons], ["not spam", "undo (moderator)"])
        for voter in (1, 2, 3):
            self.vote(voter, "clean", message_id=notice)
        self.assertEqual(self.case()["phase"], "restored")
        self.assertEqual(
            self.tg.calls_for("unbanChatMember"),
            [{"chat_id": CHAT, "user_id": 42, "only_if_banned": True}],
        )
        self.assertEqual(
            self.db.execute("SELECT exempt FROM subjects").fetchone()[0], 1
        )
        self.assertTrue(
            any(
                c["message_id"] == notice and "Unbanned and exempt" in c["text"]
                for c in self.tg.calls_for("editMessageText")
            )
        )

    def test_two_ban_notice_votes_and_expired_window_do_not_restore(self):
        self.model.verdict = "spam"
        self.send()
        notice = self.case()["ban_message"]
        for voter in (1, 1, 2):
            self.vote(voter, "clean", message_id=notice)
        self.assertEqual(self.db.execute("SELECT count(*) FROM votes").fetchone()[0], 2)
        self.assertEqual(self.case()["phase"], "banned")
        self.assertFalse(self.tg.calls_for("unbanChatMember"))
        with self.db:
            self.db.execute("UPDATE cases SET expires=?", (time.time() - 1,))
        self.vote(3, "clean", message_id=notice)
        self.assertEqual(
            self.tg.calls_for("answerCallbackQuery")[-1]["text"],
            "The undo window has closed.",
        )
        self.vote(4, "clean", message_id=notice)  # Expiry maintenance cleared expires.
        self.assertEqual(
            self.tg.calls_for("answerCallbackQuery")[-1]["text"],
            "The undo window has closed.",
        )
        self.assertEqual(self.case()["phase"], "banned")
        self.assertFalse(self.tg.calls_for("unbanChatMember"))

    def test_ban_notice_vote_restrictions(self):
        self.model.verdict = "spam"
        self.send()
        notice = self.case()["ban_message"]
        self.vote(42, "clean", message_id=notice)
        self.tg.members[5] = {"status": "left"}
        self.vote(5, "clean", message_id=notice)
        self.vote(6, "clean", message_id=99999)
        self.vote(7, "clean", message_id=notice, chat={"id": -1})
        self.vote(8, "spam", message_id=notice)
        self.assertEqual(
            self.tg.calls_for("answerCallbackQuery")[-1]["text"],
            "This vote has closed.",
        )
        self.assertEqual(self.db.execute("SELECT count(*) FROM votes").fetchone()[0], 0)
        self.assertFalse(self.tg.calls_for("unbanChatMember"))

    def test_new_spam_evidence_clears_review_votes_before_ban(self):
        self.model.verdict = "suspicious"
        self.send()
        case_id = self.case()["id"]
        for voter in (1, 2):
            self.vote(voter, "clean")
        self.model.verdict = "spam"
        self.send(2)
        self.assertEqual(self.case()["id"], case_id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM votes").fetchone()[0], 0)
        notice = self.case()["ban_message"]
        self.vote(3, "clean", message_id=notice)
        self.assertEqual(self.case()["phase"], "banned")
        self.assertFalse(self.tg.calls_for("unbanChatMember"))
        for voter in (1, 2):
            self.vote(voter, "clean", message_id=notice)
        self.assertEqual(self.case()["phase"], "restored")

    def test_pending_review_prevents_reaction_trust(self):
        self.model.verdict = "suspicious"
        for number in range(1, 11):
            self.send(number)
        self.assertFalse(self.bot.trusted(CHAT, "user:42"))
        self.bot.update(self.reaction())
        self.assertEqual(len(self.model.calls), 11)

    def test_reaction_channel_identity_and_removals(self):
        self.bot.update(
            self.reaction(user=None, actor_chat={"id": -900, "title": "Channel"})
        )
        self.assertEqual(
            self.db.execute("SELECT identity FROM observations").fetchone()[0],
            "chat:-900",
        )
        self.bot.update(self.reaction(101, new=[]))
        self.bot.update(self.reaction(102, user=None, actor_chat={"id": CHAT}))
        self.bot.update({"message_reaction_count": {"chat": {"id": CHAT}}})
        self.assertEqual(len(self.model.calls), 1)

    def test_existing_database_budget_migration(self):
        self.send()
        self.send(2, new_chat_members=[USER])
        with self.db:
            self.db.execute("ALTER TABLE observations DROP COLUMN counted")
        self.db.close()
        self.db = database(self.path)
        self.assertEqual(
            self.db.execute("SELECT sum(counted) FROM observations").fetchone()[0], 1
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
        self.assertTrue(
            any(
                f"within {UNDO_WINDOW // 3600} hours" in call["text"]
                for call in self.tg.calls_for("sendMessage")
            )
        )
        self.assertAlmostEqual(
            self.case()["expires"], time.time() + UNDO_WINDOW, delta=2
        )
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

    def test_classifier_failure_passes_as_clean(self):
        # A provider outage is not evidence: it passes silently, in the log only.
        self.model.error = APIError("Classifier", 503)
        with self.assertLogs("spam-bot", level="INFO") as logs:
            self.send()
        self.assertIsNone(self.case())
        self.assertFalse(self.tg.calls_for("sendMessage"))
        self.assertFalse(self.tg.calls_for("banChatMember"))
        self.assertIn("verdict=clean (unavailable)", logs.output[-1])

    def test_every_decision_is_logged(self):
        with self.assertLogs("spam-bot", level="INFO") as logs:
            self.send()
        line = logs.output[-1]
        self.assertIn("Decision case=-", line)
        self.assertIn("identity=user:42", line)
        self.assertIn("event=1", line)
        self.assertIn("verdict=clean", line)
        self.assertIn("media=-", line)
        self.assertIn("reason=Test evidence.", line)

    def test_profile_only_downgrade_is_logged(self):
        self.model.verdict = "spam"
        with self.assertLogs("spam-bot", level="INFO") as logs:
            self.bot.update(self.reaction())
        line = logs.output[-1]
        self.assertIn("event=reaction:900:100", line)
        self.assertIn("verdict=suspicious (profile-only)", line)
        self.assertIn("case=1", line)

    def uninspectable(self):
        """A visible profile photo that will not download leaves an inspection gap."""
        self.tg.photos = [[{"file_id": "photo"}]]
        self.tg.failures["image"] = APIError("Telegram", 400)

    def test_inspection_gap_passes_instead_of_opening_a_review(self):
        self.uninspectable()
        self.model.verdict = "suspicious"
        self.model.basis = "uninspectable"
        with self.assertLogs("spam-bot", level="INFO") as logs:
            self.send()
        self.assertIsNone(self.case())
        self.assertFalse(self.tg.calls_for("sendMessage"))
        self.assertIn("verdict=clean (uninspectable)", logs.output[-1])
        self.assertIn("media=profile image unavailable", logs.output[-1])

    def test_uninspectable_basis_without_a_gap_still_reviews(self):
        self.model.verdict = "suspicious"
        self.model.basis = "uninspectable"
        self.send()
        self.assertEqual(self.case()["phase"], "review")

    def test_any_suspicion_alongside_a_gap_passes(self):
        # The model claiming a visible indicator does not override an inspection gap:
        # a review notice costs the group a vote, a missed hunch does not.
        self.uninspectable()
        self.model.verdict = "suspicious"
        self.model.basis = "visible"
        self.send()
        self.assertIsNone(self.case())
        self.assertFalse(self.tg.calls_for("sendMessage"))

    def test_visible_spam_still_bans_despite_a_gap(self):
        self.uninspectable()
        self.model.verdict = "spam"
        self.model.basis = "visible"
        self.send()
        self.assertEqual(self.case()["phase"], "banned")

    def test_inspection_gap_never_justifies_a_ban(self):
        self.uninspectable()
        self.model.verdict = "spam"
        self.model.basis = "uninspectable"
        self.send()
        self.assertEqual(self.case()["phase"], "review")
        self.assertFalse(self.tg.calls_for("banChatMember"))

    def test_unreadable_media_passes_without_spam_evidence(self):
        self.tg.failures["image"] = APIError("Image")
        # Unavailable images alone must not trigger a moderation vote.
        self.send(photo=[{"file_id": "bad"}])
        self.assertIsNone(self.case())
        self.assertTrue(self.model.calls[0][0]["missing_media"])

    def test_unreadable_media_does_not_override_spam_evidence(self):
        self.tg.failures["image"] = APIError("Image")
        self.model.verdict = "spam"
        self.send(photo=[{"file_id": "bad"}], caption="看我简介")
        self.assertEqual(self.case()["phase"], "banned")

    def test_readable_sticker_does_not_create_unreadable_media_review(self):
        # Regression for the real group report: all stickers used to be marked missing.
        self.send(
            sticker={"file_id": "sticker", "is_animated": False, "is_video": False}
        )
        self.assertIsNone(self.case())
        evidence, images = self.model.calls[0]
        self.assertEqual(evidence["missing_media"], [])
        self.assertEqual(images[0][0], "Sticker image")

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
            self.assertEqual(payload["reasoning_effort"], "low")
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


    def test_basis_is_normalized_and_never_fails_the_response(self):
        client = Classifier("https://example.invalid/v1", "test", "test", "test")

        def completion(content):
            return {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": content}}
                ]
            }

        for content, expected in (
            ('{"verdict":"clean","reason":"x","basis":"uninspectable"}', "uninspectable"),
            ('{"verdict":"clean","reason":"x"}', "visible"),
            ('{"verdict":"clean","reason":"x","basis":"nonsense"}', "visible"),
            ('{"verdict":"clean","reason":"x","basis":null}', "visible"),
        ):
            with self.subTest(content=content), patch(
                "api.request_json", return_value=completion(content)
            ):
                self.assertEqual(client.classify({}, [])["basis"], expected)


if __name__ == "__main__":
    unittest.main()
