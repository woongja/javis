# javis

A personal AI assistant with a 3D HUD, an Obsidian second brain, and pluggable agent teams —
built by Claude Code and Codex working under a small, portable development harness.

## Status

The development harness is complete. Assistant phase 1 provides a CLI and a
Telegram channel, powered by Claude Code through your subscription login.
Both use a local worklog with source recall.

## Use javis

Run these from the checkout with `claude` on your PATH and signed in to your
Claude subscription.

```sh
uv run javis ask "<question>"
uv run javis log -n 5
uv run javis ask "방금 2번 출처"
uv run javis ask "출처 12 2"
uv run javis serve
```

`uv run javis log [-n N]` shows the latest 20 entries by default.
`방금 2번 출처` recalls item 2 from the latest successful answer.
For `출처 <ID> [N]`, use the answer's
`기록 ID` or its ID from `log`. Omit the item number to see all saved
sources for that answer. The same recall phrases work as bot messages.

The local SQLite worklog holds private requests and answers. Keep it outside
the repository. It defaults to `~/.javis`. To use a different folder, set
`JAVIS_DATA_DIR` in the shell environment before running javis.

## Telegram setup

1. In Telegram, send `/newbot` to BotFather and follow its prompts. Put the
   token in the checkout's `.env` as `JAVIS_TELEGRAM_TOKEN`. If you need to
   create `.env`, use `.env.example` as a template. [Telegram bot tutorial](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)
2. Open a private chat with your bot and send it a text message. Before starting
   `serve`, open a private browser window and visit
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`, replacing the
   placeholder including the angle brackets. In the JSON `result` array, find
   your message and copy its numeric `message.chat.id`. Keep the token confined
   to `.env` and the official HTTPS request. Never paste the token or the full
   URL into chats, issues, or third-party sites. Close the private window
   afterward. [Telegram Bot API](https://core.telegram.org/bots/api#getupdates)
3. Set `JAVIS_TELEGRAM_CHAT_ID` in `.env` to that ID. The bot accepts text
   from that private chat.
4. Run `uv run javis serve` and leave it running. Use one bot and one poller
   at a time for this setup.
5. From your phone, ask the bot for a news summary with sources. Note the
   answer's `기록 ID` and the link for item 1. Stop `serve` with Ctrl+C,
   restart it with the same data folder, and send `출처 <ID> 1` with that
   answer ID. Check that the link matches.

## Troubleshooting

- If `serve` prints `JAVIS_TELEGRAM_TOKEN` or `JAVIS_TELEGRAM_CHAT_ID`, fill in
  the named setting and make sure the chat ID is an integer.
- `Telegram 401` means Telegram rejected the token. Check it against BotFather.
- `Telegram 409` means another poller or a webhook is using the bot. Stop the
  other poller or remove the webhook, then restart `serve`. [Telegram Bot API](https://core.telegram.org/bots/api#deletewebhook)
- If javis reports `엔진 사용 한도에 도달했습니다.`, wait for your Claude usage
  limit to reset before asking again.
- If javis reports `Claude CLI 설치와 구독 로그인을 확인해 주세요.`, check that
  `claude` is on PATH and run `claude auth login` to sign in with your
  subscription. [Claude Code login help](https://code.claude.com/docs/en/troubleshooting)

## Development quick start (Windows or macOS)
```
git clone https://github.com/woongja/javis.git && cd javis
uv run harness doctor --fix --probe
uv run pytest -q
```

How agents work in this repo: see [AGENTS.md](AGENTS.md).
