# Temporary web sharing

Nova can expose its local artifact viewer, Council, Cowork and Skills & Agents
pages through a temporary ngrok HTTPS URL:

```text
/share artifacts
/share council
/share cowork
/share create
/share all
/share list
/share stop [artifacts|council|cowork|create|all]
```

Install the [ngrok agent](https://ngrok.com/download) and configure its account
token once with `ngrok config add-authtoken <token>`. Nova starts the tunnel
without opening an extra terminal window. `/share all` starts all four pages and
prints a workspace dashboard with a card for each. Sharing pages individually
also refreshes that dashboard as long as the Artifacts share is active. Each
page gets its own temporary URL; the tunnel works only while Nova and the local
page are running. Stop a link with `/share stop <page>` or stop all links with
`/share stop`.

These URLs are bearer links: anyone who receives one can open that page for as
long as the tunnel is active. Cowork and Create also carry their existing
per-process page tokens in the URL. Share links only with people you trust, and
stop them when finished. Cowork can run an agent against the current workspace,
Create can edit skills and agents, and Council can start model calls. Temporary
links use ngrok's assigned random hostname; stable domains and production
hosting are outside this feature.

The Council streams live progress and Cowork uses a WebSocket, so temporary
sharing uses ngrok's HTTP tunnel. Cloudflare Quick Tunnels are not used because
their documented limitations exclude Server-Sent Events, which Council needs.
See the [ngrok local sharing guide](https://ngrok.com/use-cases/share-localhost)
and [Cloudflare Quick Tunnel limitations](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Phone layouts

The Council, Cowork, Create and artifact viewer pages adapt their layout to
narrow screens. Nova adds viewport and overflow handling to authored HTML
artifacts and asks the artifact authoring tools to produce mobile-responsive
layouts. A fixed-width design can still need its own mobile-specific redesign.
