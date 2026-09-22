"""Telegram spam moderation. Run one process per database and bot token."""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from api import APIError, Classifier, Telegram
from media import Media

LOG = logging.getLogger("spam-bot")
UPDATES = [
    "message",
    "edited_message",
    "chat_member",
    "callback_query",
    "message_reaction",
]
ADMINS = {"creator", "administrator"}
# How long a ban notice accepts "not spam"; the notice expires with it.
UNDO_WINDOW = 5 * 3600


def database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER);
        INSERT OR IGNORE INTO settings VALUES ('offset', 0);
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY, payload TEXT NOT NULL, retry_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subjects (
            chat INTEGER, identity TEXT, exempt INTEGER DEFAULT 0,
            PRIMARY KEY (chat, identity)
        );
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, chat INTEGER, identity TEXT, event TEXT,
            message INTEGER, sent REAL, checked TEXT, counted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(chat, identity, event)
        );
        CREATE INDEX IF NOT EXISTS subject_observations
            ON observations(chat, identity, message);
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY, chat INTEGER, identity TEXT, reason TEXT,
            display_name TEXT, source_message INTEGER,
            phase TEXT, review_message INTEGER, ban_message INTEGER,
            expires REAL, deleted INTEGER DEFAULT 0, retry_at REAL DEFAULT 0,
            dirty INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS subject_cases ON cases(chat, identity, phase);
        CREATE TABLE IF NOT EXISTS votes (
            case_id INTEGER, voter INTEGER, choice TEXT,
            PRIMARY KEY(case_id, voter)
        );
    """)
    # Preserve the existing message budget when upgrading the deployed database.
    if "counted" not in {
        row[1] for row in db.execute("PRAGMA table_info(observations)")
    }:
        with db:
            db.execute(
                "ALTER TABLE observations ADD COLUMN counted INTEGER NOT NULL DEFAULT 0"
            )
            db.execute("UPDATE observations SET counted=1 WHERE event!='join'")
    return db


def identity(message):
    """sender_chat takes precedence over Telegram's fake fallback sender user."""
    if message.get("is_automatic_forward"):
        return None
    sender = message.get("sender_chat")
    if sender:
        if sender["id"] == message["chat"]["id"]:
            return None  # Anonymous administrators represent the group itself.
        return "chat:" + str(sender["id"]), sender
    sender = message.get("from")
    if sender and not sender.get("is_bot"):
        return "user:" + str(sender["id"]), sender
    return None


def is_member(member):
    return member["status"] in ADMINS | {"member"} or (
        member["status"] == "restricted" and member.get("is_member", False)
    )


class Bot:
    def __init__(self, db, telegram, classifier, chats, first_messages=10):
        self.db, self.tg, self.classifier = db, telegram, classifier
        self.chats, self.first_messages = set(chats), first_messages
        self.media = Media(telegram)

    def member(self, chat, user):
        return self.tg.call("getChatMember", chat_id=chat, user_id=user)

    def own_notice(self, chat, message):
        """The bot's notice messages are moderation UI, not member content."""
        return (
            self.db.execute(
                "SELECT 1 FROM cases WHERE chat=? AND (review_message=? OR ban_message=?) LIMIT 1",
                (chat, message, message),
            ).fetchone()
            is not None
        )

    def evidence(self, subject, sender, message):
        kind, target = subject.split(":")
        profile = {
            k: sender[k]
            for k in ("first_name", "last_name", "title", "username")
            if k in sender
        }
        profile.update(description=None, photo_status="unknown")
        images = []
        missing_media = []
        media_evidence = []
        try:
            info = self.tg.call("getChat", chat_id=int(target))
            profile["description"] = info.get("bio", info.get("description"))
        except APIError:
            info = {}  # User bios are not generally available to a group bot.
        try:
            if kind == "user":
                photos = self.tg.call(
                    "getUserProfilePhotos", user_id=int(target), limit=1
                )
                file_id = (
                    photos["photos"][0][-1]["file_id"] if photos["photos"] else None
                )
            else:
                file_id = info.get("photo", {}).get("big_file_id")
            if file_id:
                profile["photo_status"] = "visible"
                images.append(("Sender profile photo", self.tg.image(file_id)))
            elif kind == "user" or info:
                profile["photo_status"] = "no_visible_photo"
        except APIError as exc:
            if profile["photo_status"] == "visible":
                missing_media.append("profile image unavailable")
                LOG.warning("Profile image could not be inspected: %s", exc)

        if message:
            photo = message.get("photo")
            file_id = photo[-1]["file_id"] if photo else None
            if file_id:
                try:
                    images.append(("Message image", self.tg.image(file_id)))
                except APIError as exc:
                    missing_media.append("message image unavailable")
                    LOG.warning("Message photo could not be inspected: %s", exc)
            for media in (
                "document",
                "sticker",
                "video",
                "animation",
                "video_note",
                "voice",
                "audio",
            ):
                if media in message:
                    item = message[media]
                    try:
                        inspected = self.media.inspect(media, item)
                        images.extend(inspected.images)
                        media_evidence.append(
                            {
                                "kind": media,
                                "text": inspected.text,
                                "notes": inspected.notes,
                                "filename": item.get("file_name"),
                                "emoji": item.get("emoji"),
                            }
                        )
                    except APIError as exc:
                        missing_media.append(f"{media}: {exc}")
                        LOG.warning("%s could not be inspected: %s", media, exc)
        fields = (
            "text",
            "caption",
            "entities",
            "caption_entities",
            "contact",
            "poll",
            "quote",
        )
        evidence = {
            "profile": profile,
            "event": "message" if message else "join",
            "message": (
                {k: message[k] for k in fields if k in message} if message else {}
            ),
            "missing_media": missing_media,
            "media": media_evidence,
        }
        if message and message.get("reply_to_message"):
            reply = message["reply_to_message"]
            evidence["reply_context"] = {
                k: reply[k] for k in ("text", "caption") if k in reply
            }
        return evidence, images

    def trusted(self, chat, subject):
        exempt = self.db.execute(
            "SELECT exempt FROM subjects WHERE chat=? AND identity=?", (chat, subject)
        ).fetchone()
        if exempt and exempt[0]:
            return True
        checked = self.db.execute(
            "SELECT count(*) FROM observations WHERE chat=? AND identity=? AND counted=1 AND checked IS NOT NULL",
            (chat, subject),
        ).fetchone()[0]
        pending = self.db.execute(
            "SELECT 1 FROM cases WHERE chat=? AND identity=? AND phase IN ('review','ban','banned','restore') LIMIT 1",
            (chat, subject),
        ).fetchone()
        return checked >= self.first_messages and not pending

    def observe(
        self, chat, subject, sender, event, message=None, *, edited=False, reaction=None
    ):
        kind, target = subject.split(":")
        if sender.get("is_bot"):
            return
        if kind == "user" and self.member(chat, int(target))["status"] in ADMINS:
            return
        if reaction and self.trusted(chat, subject):
            return
        counted = bool(message and event != "join" and not edited)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO subjects(chat,identity) VALUES (?,?)",
                (chat, subject),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO observations(chat,identity,event,message,sent,counted) VALUES (?,?,?,?,?,?)",
                (
                    chat,
                    subject,
                    event,
                    message["message_id"] if message else None,
                    message["date"] if message else time.time(),
                    counted,
                ),
            )
            if event == "join" and message:
                self.db.execute(
                    "UPDATE observations SET message=?,sent=? WHERE chat=? AND identity=? AND event='join'",
                    (message["message_id"], message["date"], chat, subject),
                )
        if self.db.execute(
            "SELECT exempt FROM subjects WHERE chat=? AND identity=?", (chat, subject)
        ).fetchone()[0]:
            return
        case = self.db.execute(
            "SELECT * FROM cases WHERE chat=? AND identity=? AND phase IN ('ban','banned','restore') ORDER BY id DESC LIMIT 1",
            (chat, subject),
        ).fetchone()
        if case:
            if message and case["phase"] != "restore":
                self.delete(chat, message["message_id"])
            return
        observation = self.db.execute(
            "SELECT * FROM observations WHERE chat=? AND identity=? AND event=?",
            (chat, subject, event),
        ).fetchone()
        # Edits of even unseen old messages are checked only inside the initial
        # observation window. They never consume an original-message slot.
        if (
            edited
            and self.db.execute(
                "SELECT count(*) FROM observations WHERE chat=? AND identity=? AND counted=1",
                (chat, subject),
            ).fetchone()[0]
            >= self.first_messages
        ):
            return
        if counted:
            ordinal = self.db.execute(
                "SELECT count(*) FROM observations WHERE chat=? AND identity=? AND counted=1 AND id<=?",
                (chat, subject, observation["id"]),
            ).fetchone()[0]
            if ordinal > self.first_messages:
                return
        digest = hashlib.sha256(
            json.dumps(
                reaction or (message if event != "join" else sender), sort_keys=True
            ).encode()
        ).hexdigest()
        if observation["checked"] == digest:
            return
        try:
            evidence, images = self.evidence(
                subject, sender, message if event != "join" else None
            )
            if reaction:
                evidence["event"] = "reaction"
                evidence["reaction"] = reaction["new_reaction"]
                evidence["context"] = (
                    "Check the reacting identity, not the author of the reacted-to message."
                )
            elif edited:
                evidence["event"] = "edited_message"
            result = self.classifier.classify(evidence, images)
            # Code-level guardrail: the model cannot authorize profile-only bans.
            profile_only = reaction is not None or event == "join"
            if result["verdict"] == "spam" and profile_only:
                result = {
                    "verdict": "suspicious",
                    "reason": (
                        "Profile-only evidence cannot justify an automatic ban; human "
                        "review needed. " + result["reason"]
                    )[:300],
                }
        except APIError as exc:
            LOG.warning("Classification unavailable: %s", exc)
            result = {
                "verdict": "suspicious",
                "reason": "Automatic classification unavailable; human review needed.",
            }
        with self.db:
            active = self.db.execute(
                "SELECT id FROM cases WHERE chat=? AND identity=? AND phase='review'",
                (chat, subject),
            ).fetchone()
            if result["verdict"] != "clean":
                phase = "ban" if result["verdict"] == "spam" else "review"
                if active and phase == "ban":
                    # Review votes must not count toward undoing new spam evidence.
                    self.db.execute("DELETE FROM votes WHERE case_id=?", (active[0],))
                    self.db.execute(
                        "UPDATE cases SET phase='ban',reason=?,dirty=1 WHERE id=?",
                        (result["reason"], active[0]),
                    )
                elif not active:
                    name = sender.get("title") or " ".join(
                        sender.get(k, "") for k in ("first_name", "last_name")
                    )
                    self.db.execute(
                        "INSERT INTO cases(chat,identity,reason,phase,display_name,source_message) VALUES (?,?,?,?,?,?)",
                        (
                            chat,
                            subject,
                            result["reason"],
                            phase,
                            " ".join(name.split())[:80],
                            (
                                message["message_id"]
                                if message
                                else reaction["message_id"] if reaction else None
                            ),
                        ),
                    )
            self.db.execute(
                "UPDATE observations SET checked=? WHERE id=?",
                (digest, observation["id"]),
            )

    def update(self, update):
        if "callback_query" in update:
            self.vote(update["callback_query"])
            return
        reaction = update.get("message_reaction")
        if reaction:
            chat = reaction["chat"]["id"]
            if chat not in self.chats or not any(
                item not in reaction["old_reaction"]
                for item in reaction["new_reaction"]
            ):
                return
            if self.own_notice(chat, reaction["message_id"]):
                return  # Reactions to moderation UI are not member content.
            actor = identity(
                {
                    "chat": reaction["chat"],
                    "sender_chat": reaction.get("actor_chat"),
                    "from": reaction.get("user"),
                }
            )
            if actor:
                self.observe(
                    chat,
                    *actor,
                    f"reaction:{reaction['message_id']}:{update['update_id']}",
                    reaction=reaction,
                )
            return
        member = update.get("chat_member")
        if member:
            chat = member["chat"]["id"]
            if (
                chat in self.chats
                and not is_member(member["old_chat_member"])
                and is_member(member["new_chat_member"])
            ):
                sender = member["new_chat_member"]["user"]
                # Both chat_member and new_chat_members may describe the same join.
                self.observe(chat, "user:" + str(sender["id"]), sender, "join")
            return
        message = update.get("message", update.get("edited_message"))
        if not message or message["chat"]["id"] not in self.chats:
            return
        chat = message["chat"]["id"]
        if message.get("new_chat_members"):
            for sender in message["new_chat_members"]:
                self.observe(chat, "user:" + str(sender["id"]), sender, "join", message)
            return  # The inviter is not the subject of a join service message.
        if message.get("left_chat_member") or not message.get("message_id"):
            return
        subject = identity(message)
        if subject:
            edited = "edited_message" in update
            self.observe(
                chat,
                *subject,
                (
                    f"edit:{message['message_id']}"
                    if edited
                    else str(message["message_id"])
                ),
                message,
                edited=edited,
            )

    def vote(self, query):
        """One immutable vote per current member; one moderator click to restore."""

        def answer(text):
            try:
                self.tg.call(
                    "answerCallbackQuery", callback_query_id=query["id"], text=text
                )
            except APIError:
                pass  # A delayed callback may have expired; durable vote still counts.

        parts = query.get("data", "").split(":")
        if (
            len(parts) != 3
            or parts[0] != "case"
            or not parts[1].isdigit()
            or parts[2] not in {"spam", "clean", "undo"}
        ):
            answer("Invalid button.")
            return
        case = self.db.execute(
            "SELECT * FROM cases WHERE id=?", (int(parts[1]),)
        ).fetchone()
        message = query.get("message", {})
        choice = parts[2]
        expected = (
            case[
                (
                    "ban_message"
                    if choice == "undo" or case["phase"] == "banned"
                    else "review_message"
                )
            ]
            if case
            else None
        )
        if (
            not case
            or case["chat"] not in self.chats
            or message.get("chat", {}).get("id") != case["chat"]
            or message.get("message_id") != expected
        ):
            answer("This button is not valid here.")
            return
        voter = query["from"]
        if voter.get("is_bot") or case["identity"] == "user:" + str(voter["id"]):
            answer("You cannot vote on your own case.")
            return
        membership = self.member(case["chat"], voter["id"])
        if choice == "undo":
            if not (
                membership["status"] == "creator"
                or (
                    membership["status"] == "administrator"
                    and membership.get("can_restrict_members")
                )
            ):
                answer("A moderator with ban permissions must undo this decision.")
                return
            if (
                case["phase"] != "banned"
                or not case["expires"]
                or case["expires"] <= time.time()
            ):
                answer("The undo window has closed.")
                return
            with self.db:
                self.db.execute(
                    "UPDATE cases SET phase='restore',retry_at=0 WHERE id=?",
                    (case["id"],),
                )
            answer("Restoration queued. The notice will confirm success.")
            return
        if not is_member(membership):
            answer("Only current group members can vote.")
            return
        if case["phase"] != "review" and not (
            case["phase"] == "banned" and choice == "clean"
        ):
            answer("This vote has closed.")
            return
        if case["phase"] == "banned" and (
            not case["expires"] or case["expires"] <= time.time()
        ):
            answer("The undo window has closed.")
            return
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO votes VALUES (?,?,?)",
                (case["id"], voter["id"], choice),
            ).rowcount
            count = self.db.execute(
                "SELECT count(*) FROM votes WHERE case_id=? AND choice=?",
                (case["id"], choice),
            ).fetchone()[0]
            phase = case["phase"]
            if count >= 3:
                phase = (
                    "restore"
                    if case["phase"] == "banned"
                    else "ban" if choice == "spam" else "clean"
                )
            self.db.execute(
                "UPDATE cases SET phase=?,dirty=1,retry_at=0 WHERE id=?",
                (phase, case["id"]),
            )
        answer("Vote recorded." if inserted else "You have already voted.")

    def delete(self, chat, message):
        try:
            self.tg.call("deleteMessage", chat_id=chat, message_id=message)
        except APIError as exc:
            if (
                exc.code == 400
                and "message to delete not found" in exc.description.lower()
            ):
                return
            raise

    def notice(self, case, text, buttons, field):
        params = {
            "chat_id": case["chat"],
            "text": text,
            "reply_markup": {"inline_keyboard": buttons},
            "link_preview_options": {"is_disabled": True},
        }
        if case[field]:
            try:
                self.tg.call("editMessageText", message_id=case[field], **params)
            except APIError as exc:
                if (
                    exc.code == 400
                    and "message is not modified" in exc.description.lower()
                ):
                    return
                if (
                    exc.code == 400
                    and "message to edit not found" in exc.description.lower()
                ):
                    if field == "review_message" and case["phase"] == "review":
                        self.notice({**dict(case), field: None}, text, buttons, field)
                    return
                raise
        else:
            if field == "review_message" and case["source_message"]:
                params["reply_parameters"] = {
                    "message_id": case["source_message"],
                    "allow_sending_without_reply": True,
                }
            sent = self.tg.call("sendMessage", **params)
            with self.db:
                if field == "ban_message":
                    self.db.execute(
                        "UPDATE cases SET ban_message=?,expires=? WHERE id=?",
                        (sent["message_id"], time.time() + UNDO_WINDOW, case["id"]),
                    )
                else:
                    self.db.execute(
                        "UPDATE cases SET review_message=? WHERE id=?",
                        (sent["message_id"], case["id"]),
                    )

    def advance(self, case):
        """Persist intent before API mutations; retry unfinished work after restart."""
        case_id, chat = case["id"], case["chat"]
        kind, target = case["identity"].split(":")
        target = int(target)
        phase = case["phase"]
        if phase == "ban":
            if kind == "user" and self.member(chat, target)["status"] in ADMINS:
                with self.db:
                    self.db.execute(
                        "UPDATE cases SET phase='clean',dirty=1 WHERE id=?", (case_id,)
                    )
                return
            if kind == "user":
                self.tg.call(
                    "banChatMember", chat_id=chat, user_id=target, revoke_messages=True
                )
            else:
                self.tg.call("banChatSenderChat", chat_id=chat, sender_chat_id=target)
            with self.db:
                self.db.execute(
                    "UPDATE cases SET phase='banned',dirty=1 WHERE id=?", (case_id,)
                )
            phase = "banned"
        if phase == "restore":
            if kind == "user":
                self.tg.call(
                    "unbanChatMember", chat_id=chat, user_id=target, only_if_banned=True
                )
            else:
                self.tg.call("unbanChatSenderChat", chat_id=chat, sender_chat_id=target)
            with self.db:
                self.db.execute(
                    "UPDATE subjects SET exempt=1 WHERE chat=? AND identity=?",
                    (chat, case["identity"]),
                )
                self.db.execute(
                    "UPDATE cases SET phase='restored',dirty=1 WHERE id=?", (case_id,)
                )
            phase = "restored"
        label = f"{case['display_name']} — {case['identity']} (case {case_id})"

        def button(text, action):
            return {"text": text, "callback_data": f"case:{case_id}:{action}"}

        if phase == "review" and case["dirty"]:
            counts = dict(
                self.db.execute(
                    "SELECT choice,count(*) FROM votes WHERE case_id=? GROUP BY choice",
                    (case_id,),
                ).fetchall()
            )
            self.notice(
                case,
                f"Suspected spam: {label}\n{case['reason']}\nFirst to 3 distinct member votes wins.\nspam: {counts.get('spam', 0)} | not spam: {counts.get('clean', 0)}",
                [[button("spam", "spam"), button("not spam", "clean")]],
                "review_message",
            )
        elif phase in {"banned", "clean", "restored"}:
            if phase == "banned" and not case["ban_message"]:
                self.notice(
                    case,
                    f"Banned for spam: {label}\n{case['reason']}\nA moderator can select undo within {UNDO_WINDOW // 3600} hours to unban and exempt this identity; three member votes for not spam do the same. Deleted messages cannot be restored.",
                    [[button("not spam", "clean"), button("undo (moderator)", "undo")]],
                    "ban_message",
                )
            if phase == "restored" and case["ban_message"] and case["dirty"]:
                self.notice(
                    case,
                    f"Unbanned and exempt in this group: {label}\nThey may rejoin. Deleted messages cannot be restored.",
                    [],
                    "ban_message",
                )
            if phase == "banned" and not case["deleted"]:
                # Users get server-side history revocation. Sender chats have no such
                # option: delete every observed message still within the API window.
                messages = self.db.execute(
                    "SELECT message FROM observations WHERE chat=? AND identity=? AND message IS NOT NULL AND sent>?",
                    (chat, case["identity"], time.time() - 48 * 3600),
                ).fetchall()
                for message in messages:
                    self.delete(chat, message[0])
                self.tg.call(
                    "deleteAllMessageReactions",
                    chat_id=chat,
                    **{"user_id" if kind == "user" else "actor_chat_id": target},
                )
                with self.db:
                    self.db.execute("UPDATE cases SET deleted=1 WHERE id=?", (case_id,))
            if case["review_message"] and case["dirty"]:
                self.notice(
                    case,
                    f"Review closed: {label}\nDecision: {phase}.",
                    [],
                    "review_message",
                )
        with self.db:
            self.db.execute("UPDATE cases SET dirty=0 WHERE id=?", (case_id,))

    def maintain(self):
        # Notice expiry is independent of history-deletion or notice-edit failures.
        for case in self.db.execute(
            "SELECT * FROM cases WHERE expires<=? AND retry_at<=?",
            (time.time(), time.time()),
        ).fetchall():
            if case["chat"] not in self.chats:
                continue
            try:
                self.delete(case["chat"], case["ban_message"])
                with self.db:
                    # Keep the ID to distinguish an expired notice from one not sent.
                    self.db.execute(
                        "UPDATE cases SET expires=NULL WHERE id=?", (case["id"],)
                    )
            except APIError as exc:
                LOG.warning("Notice for case %s could not expire: %s", case["id"], exc)
                with self.db:
                    self.db.execute(
                        "UPDATE cases SET retry_at=? WHERE id=?",
                        (time.time() + max(10, exc.retry_after), case["id"]),
                    )
        for case in self.db.execute(
            "SELECT * FROM cases WHERE retry_at<=? AND (dirty=1 OR phase IN ('ban','restore') OR (phase='banned' AND (ban_message IS NULL OR deleted=0)))",
            (time.time(),),
        ).fetchall():
            if case["chat"] not in self.chats:
                continue
            try:
                self.advance(case)
            except APIError as exc:
                LOG.warning("Case %s remains pending: %s", case["id"], exc)
                with self.db:
                    self.db.execute(
                        "UPDATE cases SET retry_at=? WHERE id=?",
                        (time.time() + max(10, exc.retry_after), case["id"]),
                    )

    def receive(self, updates):
        """Durably save updates before acknowledging their offset to Telegram."""
        with self.db:
            for update in updates:
                self.db.execute(
                    "INSERT OR IGNORE INTO inbox(id,payload) VALUES (?,?)",
                    (update["update_id"], json.dumps(update)),
                )
            if updates:
                self.db.execute(
                    "UPDATE settings SET value=? WHERE key='offset'",
                    (max(u["update_id"] for u in updates) + 1,),
                )

    def drain(self):
        # Serial processing makes the third vote decisive and keeps SQLite simple.
        for item in self.db.execute(
            "SELECT * FROM inbox WHERE retry_at<=? ORDER BY id LIMIT 20", (time.time(),)
        ).fetchall():
            try:
                self.update(json.loads(item["payload"]))
                with self.db:
                    self.db.execute("DELETE FROM inbox WHERE id=?", (item["id"],))
            except APIError as exc:
                LOG.warning("Update %s remains pending: %s", item["id"], exc)
                with self.db:
                    self.db.execute(
                        "UPDATE inbox SET retry_at=? WHERE id=?",
                        (time.time() + max(10, exc.retry_after), item["id"]),
                    )
            self.maintain()

    def run(self):
        me = self.tg.call("getMe")
        if self.tg.call("getWebhookInfo").get("url"):
            raise SystemExit(
                "This bot has a webhook. Use a dedicated bot with polling enabled."
            )
        for chat in self.chats:
            info = self.tg.call("getChat", chat_id=chat)
            member = self.member(chat, me["id"])
            if (
                info["type"] != "supergroup"
                or member["status"] != "administrator"
                or not all(
                    member.get(right)
                    for right in ("can_delete_messages", "can_restrict_members")
                )
            ):
                raise SystemExit(
                    f"Chat {chat} must be a supergroup; grant the bot delete and ban permissions."
                )
        LOG.info("Polling %d supergroup(s)", len(self.chats))
        while True:
            self.drain()
            self.maintain()
            offset = self.db.execute(
                "SELECT value FROM settings WHERE key='offset'"
            ).fetchone()[0]
            pending = self.db.execute(
                "SELECT 1 FROM inbox WHERE retry_at<=? LIMIT 1", (time.time(),)
            ).fetchone()
            try:
                self.receive(
                    self.tg.call(
                        "getUpdates",
                        offset=offset,
                        timeout=0 if pending else 10,
                        allowed_updates=UPDATES,
                    )
                )
            except APIError as exc:
                LOG.warning("Polling failed: %s", exc)
                time.sleep(max(1, min(exc.retry_after, 60)))


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    required = (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_IDS",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    )
    missing = [
        key
        for key in required
        if not os.environ.get(key) or os.environ[key] == "replace-me"
    ]
    if missing:
        raise SystemExit("Configure: " + ", ".join(missing))
    chats = {int(value.strip()) for value in os.environ["TELEGRAM_CHAT_IDS"].split(",")}
    first_messages = int(os.getenv("FIRST_MESSAGES", "10"))
    if first_messages < 1:
        raise SystemExit("FIRST_MESSAGES must be positive")
    path = Path(os.getenv("DATABASE_PATH", "spam-bot.sqlite3"))
    os.umask(0o077)
    with open(str(path) + ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another process is using this database") from None
        db = database(path)
        try:
            Bot(
                db,
                Telegram(os.environ["TELEGRAM_BOT_TOKEN"]),
                Classifier(
                    os.environ["OPENAI_BASE_URL"],
                    os.environ["OPENAI_API_KEY"],
                    os.environ["OPENAI_MODEL"],
                    os.getenv(
                        "GROUP_CONTEXT",
                        "General discussion; unsolicited advertising is not permitted.",
                    ),
                ),
                chats,
                first_messages,
            ).run()
        except KeyboardInterrupt:
            LOG.info("Stopped")
        except APIError as exc:
            raise SystemExit(str(exc)) from None
        finally:
            db.close()


if __name__ == "__main__":
    main()
