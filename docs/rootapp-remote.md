# Rootapp remote

Rootapp connects a Root community channel to your running Nova session. Nova
launches the official Root SDK DevHost locally, so this connection needs no
public tunnel. Keep Nova running while you use the bot.

## Set up Root

Follow [Root's bot setup guide](https://docs.rootapp.com/docs/bot-docs/get-started/):

1. Install the Root client and create a bot in the Root Developer Portal.
2. Add the bot to your development community and generate a **Dev Token** for it.
3. Copy the **Bot ID** (the manifest `id`) and the channel ID. The bot must be
   allowed to read and send messages in that channel.
4. Install **Node.js 22+** with npm on the computer running Nova.

You do not need to generate or build the starter bot yourself. Nova ships its
own companion and installs its locked SDK dependencies in `~/.nova/rootapp`.
This integration uses Root's local development host, rather than publishing
your Nova session as a Root Store bot.

## Connect from Nova

Open `/remote` and select **Setup Rootapp** to install the optional Node SDK.
Enter your Dev Token in the password field, Bot ID and channel ID, then select
**Start Rootapp**. Under sender access, choose **Rootapp**, enter your personal
Root user ID and select **Authorize**.

The equivalent local commands are:

```text
/remote setup rootapp
/remote start rootapp --token DEV_TOKEN --bot-id BOT_ID --channel CHANNEL_ID --user-id USER_ID
```

If you do not know your personal user ID, send `/nova hello` in the configured
Root channel and read the blocked-sender notice in Nova. Authorize that ID locally:

```text
/remote allow-user rootapp USER_ID
```

The Dev Token, IDs and sender permissions are saved using Nova's existing
restricted remote configuration file. On later sessions, use:

```text
/remote start rootapp
```

You can also supply the token through `ROOTAPP_DEV_TOKEN` instead of `--token`.
The saved token takes precedence over that environment variable.

## Use it in Root

Prefix requests with `/nova`:

```text
/nova explain this project's architecture
/nova /status
/nova /stop
/nova @tab:my-session fix the failing test
```

Nova uses its existing remote command handling, session routing, question flow,
and separate editable messages for answers and tool progress. Replying to an
outgoing session message routes your next `/nova` request back to that session.
Use ordinary Markdown text; Rootapp attachments and voice notes are not yet
imported into Nova.

Other channels and unapproved sender IDs are ignored. To revoke a user:

```text
/remote deny-user rootapp USER_ID
```

To inspect or end the connection:

```text
/remote status
/remote test
/remote stop rootapp:CHANNEL_ID
/remote forget rootapp
```

`/remote stop` disconnects all remote bridges. Closing Nova also stops the
DevHost and removes its temporary connection secret. Forgetting credentials
does not disconnect an already running bridge; stop it first when revoking access.

If startup fails, check Node/npm installation, your Dev Token and its development
community, the Bot ID and channel permissions. Run setup again after upgrading
Nova if the required SDK version changes.

Nova's locked companion overrides the SDK's older transitive SQLite, UUID and
tar packages with patched versions. After updating Nova, run `/remote setup rootapp`
again to install those fixes in your existing optional SDK environment. The
updated SDK was checked by loading the official module and performing local
SQLite operations; connecting to your live Root community still needs your
Dev Token and account permissions.
