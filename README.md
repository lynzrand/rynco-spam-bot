# Telegram spam bot

A small Python 3.11+ bot using the Telegram Bot API, an OpenAI-compatible vision
Chat Completions endpoint, and SQLite. No third-party Python packages or containers
are required. Media extraction uses FFmpeg, Poppler, and whisper.cpp.
The configured provider is **DeepSeek 4.1 Flash on OpenCode Go**:
`https://opencode.ai/zen/go/v1`, model `deepseek-flash`.
Classifier requests explicitly set `reasoning_effort: low`, using DeepSeek's
[documented thinking control](https://api-docs.deepseek.com/guides/thinking_mode/).

## Run

1. Create a dedicated bot with BotFather. Add it as an administrator to each target
   **supergroup**, with **Delete messages** and **Ban users** permissions. For channel
   comments, add it to the channel's **linked discussion supergroup**.
2. Copy `.env.example` to `.env` if `.env` does not already exist; `chmod 600 .env`.
   Set its Telegram bot token, numeric group IDs, and model API credentials.
   `TELEGRAM_CHAT_IDS` is a comma-separated allowlist. Set `GROUP_CONTEXT` to the
   group's topic and permitted advertising. Keep credentials in the private `.env`;
   never commit it.
3. Launch from this directory:

   ```sh
   set -a
   . ./.env
   set +a
   python3 bot.py
   ```

The bot does not load `.env` itself. Keep shell tracing off when loading credentials.
Use one process and one database per bot token. Startup checks permissions and
refuses an existing webhook. Ctrl-C stops it.
Use a test group first; automatic spam verdicts really ban and delete messages.

`rynco-spam-bot.service` is a user systemd unit for Camellia. It uses the private
`.env`, the host Python interpreter, and the existing HTTP proxy. The host's resident
assistant installs/enables it; inspect it with
`systemctl --user status rynco-spam-bot.service` and
`journalctl --user -u rynco-spam-bot.service`. Do not run a second polling process
while the service is active.

OpenCode Go [documents coding-agent traffic as its intended usage][go]. This client
identifies itself honestly as `rynco-spam-bot/0.1` and sends a session ID. API
acceptance does not establish that sustained moderation traffic is supported under
that subscription. The endpoint, API key, and model can all be changed without code.
The provider must accept image data URLs, JSON mode (`response_format: json_object`),
and Chat Completions responses. There is no silent fallback to another paid provider.

## Behavior

- Check the first **10 distinct observed messages per identity per group** (adjust
  `FIRST_MESSAGES`), plus the first observed join/profile event. Join messages do
  not consume the ten-message budget. Repeated join updates with unchanged user
  fields are deduplicated. Edits, including edits of previously unseen old messages,
  are checked while fewer than 10 original messages have been observed; they never
  consume a slot. Once 10 messages are observed, edits are no longer checked.
  Album items are separate messages and each consumes a slot.
- Reaction additions/changes trigger profile checks for untrusted reacting users or
  channel identities, without consuming a message slot. An identity becomes trusted
  for reactions after 10 checked original messages with no unresolved moderation
  case, or through explicit exemption. Reaction removals and anonymous aggregate
  counts are ignored. Telegram must expose `user` or `actor_chat`; the underlying
  post's author is never treated as the reactor. This is separate from review-button
  voting. A ban also removes that identity's recent reactions using Telegram's
  `deleteAllMessageReactions` (up to 10000), never the innocent reacted-to messages.
- Prefer `sender_chat` over Telegram's placeholder `from` user. Channel identities
  have their own counters. Skip bots, administrators, anonymous administrators
  speaking as the group, and automatic forwards from the linked channel.
- Inspect display names, usernames, accessible bio/description, latest visible
  profile photo, text, captions, link entities, reply text, Telegram photos, and
  image documents and extracted media (see below). Missing visible photo adds caution
  in the prompt but cannot justify a flag alone. Download photos locally (8 MiB maximum
  each; other attachments up to 20 MiB) and send base64
  data; the model provider never receives Telegram token-bearing download URLs.
- **Spam:** persist the decision, ban with the appropriate user/sender-chat method,
  and delete messages. User bans request server-side history revocation. Sender-chat
  bans require deleting observed messages individually. Post a ban notice with a
  **not spam** button that lasts 30 minutes from posting.
- **Suspicious:** post **spam / not spam** buttons. The first side to reach **three
  distinct current group members** wins. One immutable vote per person; no self-votes
  or forwarded/cross-chat buttons. A not-spam vote closes that case without exempting
  the remaining initial messages. Pending reviews have no time limit. New clear spam
  evidence can supersede an unresolved review.
- **Undo:** one click by the group owner or an administrator with ban permissions
  unbans and permanently exempts that identity **in this group**. Ordinary members
  cannot undo bans. Exemption is saved only after the unban succeeds. The person may
  rejoin; Telegram cannot restore deleted messages. A sender-chat exemption cannot
  identify or exempt its hidden owner or the owner's other channels.
- Classifier errors, invalid model output, or an otherwise clean message with
  unreadable/unsupported media go to human review, never an automatic ban based on
  the error alone. Inspection failures identify the media type and failing decoder;
  logs omit media contents and credentials. Profile metadata unavailable through
  Telegram remains unknown.

## Media inspection

Install FFmpeg (`ffmpeg`, `ffprobe`), Poppler (`pdfinfo`, `pdftoppm`), and whisper.cpp
(`whisper-cli`). Place the multilingual base model at `models/ggml-base.bin` or set
`WHISPER_MODEL_PATH`; the directory is gitignored. On Camellia these tools and the
model were provisioned by the host assistant. `.env.example` lists optional path
overrides. Each decoder uses at most two threads; Whisper loads on demand, with no
resident transcription server. Temporary attachment files are deleted after use.

| Media | Inspection |
| --- | --- |
| Static stickers | Full visible sticker image |
| TGS animated stickers | Telegram's thumbnail preview, explicitly labelled partial |
| Video stickers, videos, video notes, GIFs | Up to six sampled frames across the clip |
| Voice, audio, video soundtracks | Local multilingual Whisper transcription of the first 120 seconds |
| Image documents | Decoded still image or sampled animation frames |
| PDFs, including scanned pages | First six pages rendered for vision |
| DOCX/XLSX/PPTX, ODT/ODS/ODP | Extracted XML text and up to six embedded images |
| UTF-8/UTF-16 text, Markdown, CSV, JSON, XML, HTML, logs | Up to 16000 text characters; never executed |

Sampling is not exhaustive inspection. Short-lived video text, later PDF pages,
speech after two minutes, Office layout, and TGS frames outside the preview can be
missed. Recognition errors do not by themselves establish spam. Unknown binary
formats (including legacy `.doc`/`.xls`), encrypted/corrupt documents, unavailable
previews, and over-limit attachments still require review. Office expansion is
limited to 32 MiB/2000 ZIP entries and image/video dimensions to 16 million pixels.
Decoder calls time out after 30 seconds (Whisper: 90 seconds).

## Telegram constraints and operational limits

The **commenter** need not join the discussion group; the **bot must be an admin
there**. A channel-only bot cannot moderate its discussion group. The Bot API has
no general arbitrary-user bio lookup or history enumeration. `getChat(user_id)` is
best effort; users who have not opened a private conversation with the bot may be
inaccessible. Profile photos may be hidden by privacy settings.

For sender-chat identities, deletion is limited to observed message IDs still
inside Telegram's 48-hour deletion window. Earlier unobserved sender-chat messages
cannot be purged with this API. Telegram also documents that banning a sender chat
prevents its owner from posting through their other channels until unbanned. The
bot cannot uncover anonymous administrators' real identities and deliberately skips
the group's own sender identity. See the [Telegram API reference][telegram].

SQLite retains counters via observation IDs, votes, decisions, exemptions, and the
polling offset. Incoming updates are saved before acknowledgment and removed after
processing. Failed Telegram actions are retried; restart resumes pending work and
removes expired ban notices. Only allowlisted groups are acted on. Temporary API
failures can delay deletion/expiry. Decisions and API side effects cannot form a
single transaction: a crash after sending a notice but before saving its ID may
leave a duplicate/orphan notice; ban and unban retries are otherwise idempotent.

This is a serial worker for small groups: model/API latency delays callbacks and
notice cleanup. Telegram retains undelivered updates for at most 24 hours, so long
downtime or sustained overload can miss messages. The database retains message IDs
and case reasons indefinitely; it temporarily contains full queued updates. Raw
media bytes are not retained after extraction. Visible profile data and message content go to the
configured model provider. Keep the database and `.env` private. No message history
from before the bot started is imported. Three-member voting does not prevent
collusion by three accounts.

## Verification

```sh
python3 -m unittest -v
python3 -m py_compile api.py bot.py media.py test_bot.py test_media.py smoke_test.py
```

Tests use fake Telegram calls and a temporary database. They cover counting/edits,
join deduplication, non-member comments, channel identities, voting authorization,
restart recovery, ban/unban failures, expiry, and multimodal request validation.
Real decoder tests generate small synthetic video/GIF/PDF/audio fixtures. These
tests skip with an explicit reason if their host tools/model are not installed.
They do not measure model accuracy or prove Telegram integration with a real group.

After loading `.env`, `python3 smoke_test.py` sends eight synthetic cases to the
configured provider (uses API quota): a Chinese question without an avatar, a
task/rebate scam, scam warnings, a PNG image, profile funnels, and an advertising
account greeting. It checks expected verdicts and
exits nonzero on a mismatch or API error. This is a small integration smoke test,
not an accuracy benchmark. Source formatting uses Black.

## Prompt research

`system_prompt.txt` is editable English policy with Chinese examples. It treats
messages, bios, image text, and quoted replies as evidence, never instructions.
Advertising accounts are spam even without fraud: a greeting or join does not
excuse an advertising name, bio, or profile image. A sender's own "看我简介"
profile funnel is spam even when the bio is unavailable. Quoting/reporting that
wording and factual occupations remain distinct from promotion. These decisions
use the model prompt, not a separate keyword filter.

- [Chinese police: 20 fraud patterns][patterns] informed task/rebate scams,
  sexual-advertising funnels, unknown QR links, fake support, guaranteed-return
  bait, and money-mule recruitment. The prompt uses behavior examples, not the
  article's blanket legal or cryptocurrency claims.
- [OpenRouter's DeepSeek vision overview][vision] distinguishes native 4.1 Flash
  image support from older text-only V4 variants. Endpoint-specific acceptance is
  still tested separately.
- [OpenAI image-input documentation][images] specifies the Chat Completions image
  data-URL format used by compatible providers.
- The obfuscation and prompt-injection rules are implementation policy, not a claim
  that any one keyword or writing style proves spam.

[go]: https://opencode.ai/docs/go/
[telegram]: https://core.telegram.org/bots/api
[patterns]: https://legalinfo.moj.gov.cn/zhfxfzzx/fzzxyw/202506/t20250620_521303.html
[vision]: https://openrouter.ai/blog/insights/deepseek-v4-vision/
[images]: https://developers.openai.com/api/docs/guides/images-vision
