# Discord session threads

Discord now mirrors Nova's Telegram session topics using public threads in a
server text channel, or posts in a forum channel. Each running Nova session gets
its own thread, including the main session and additional session tabs.

## Connect

1. Enable **Message Content Intent** for your bot in the Discord Developer Portal.
2. Give the bot access to a server text or forum channel. For text channels,
   grant **Create Public Threads**, **Send Messages**, **Send Messages in Threads**,
   **View Channel** and **Read Message History**. Forum posts require permission
   to send messages in the forum and its threads; use a forum without mandatory
   tags. **Manage Threads** lets the bot reopen moderator-locked threads.
3. Open `/remote`, enter the bot token and parent channel ID, then **Start Discord**.
4. Under sender access, select **Discord**, enter your personal Discord user ID
   and select **Authorize**.

You can also use the local commands:

```text
/remote allow-user discord YOUR_USER_ID
/remote start discord --token BOT_TOKEN --channel PARENT_CHANNEL_ID
```

Credentials and channel settings are saved as before. Subsequent connections
can use `/remote start discord`. No new dependency or separate bot setup is needed.

## Use the threads

Nova creates a thread for the main session and each open session tab. Creating
another session tab creates its thread automatically. Regular text channels get
a link to each new thread in the parent channel; forum posts appear in the forum.

Send a prompt directly inside the thread. It routes to that session automatically,
with replies, progress messages and streamed answers staying in the thread.
The existing remote slash commands and explicit `@tab:name` routing remain
available. Local work in Nova also streams into the matching session thread.
Terminal prompts appear in full as **You · terminal** messages, followed by live
tool activity and Nova's streamed reply. This also applies to local prompts in
child session tabs. Connecting Discord during a running turn attaches its current
prompt and response to the session thread automatically.

`/remote status` and the native `/remote` screen show the active thread IDs.
Session tabs display **DC** when connected.

## History and lifecycle

- Reconnecting or resuming the same saved session reuses its thread and history.
- Renaming a session updates the existing thread's title.
- Closing a session tab archives its thread. Resuming the session reopens it.
- Switching the main session to a different saved session archives its former thread.
- Discord may archive inactive threads automatically; Nova reopens them when
  the session is used again.
- Deleted threads are recreated when Nova next synchronizes that session.
- A permissions error preserves the existing binding rather than creating a
  duplicate or sending that session's output into another thread.

Thread mappings are stored in Nova's durable store, scoped to the bot and parent
channel. Other threads under the parent channel do not gain access to Nova, and
unapproved Discord users are ignored even inside Nova's own threads.

Threads inherit the parent channel's visibility. An approved user can continue
to address another session explicitly, as with Telegram.

If thread creation is unavailable, the main session can still use a regular
parent text channel. Direct messages and an explicitly configured existing
thread keep their original behavior; Nova cannot create nested threads there.

`/remote stop` disconnects the bot. It preserves thread history and mappings for
the next connection.

## Troubleshooting

If Nova reports that the parent channel is unavailable (HTTP 404, Discord code
10003 / Unknown Channel), Discord cannot resolve the saved channel for this bot.
It may have been deleted, the saved ID may be incorrect, or the bot may lack
access. This happens before Nova can create a session thread.

Enable Developer Mode in Discord, right-click the intended text or forum channel
and select **Copy Channel ID**. Use that ID in `/remote`, then stop and start the
Discord connection. A server ID or message ID cannot be used as the parent channel.
Ensure the bot is in that server and has **View Channel** access, including any
channel-specific permission overrides. Nova does not create a replacement parent
channel or redirect session messages when the configured channel is unavailable.

References: [Discord threads](https://docs.discord.com/developers/topics/threads)
and [discord.py channel/thread APIs](https://discordpy.readthedocs.io/en/stable/api.html).
