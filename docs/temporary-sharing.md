# Temporary web sharing

Nova can expose session artifacts through a temporary public HTTPS URL:

```text
/share artifacts
/share all
/share list
/share stop [artifacts|all]
```

Nova prefers Cloudflare Quick Tunnels for artifacts. Install
[`cloudflared`](https://developers.cloudflare.com/tunnel/downloads/) to use a
free temporary `trycloudflare.com` URL without an account or custom domain.
If `cloudflared` is unavailable, Nova falls back to ngrok. The link works only
while Nova and the local artifact viewer are running; `/share stop` revokes it.

Anyone with the URL can open the shared artifacts while the tunnel is active.
Cloudflare Quick Tunnel URLs change when restarted and have no uptime guarantee.
They also do not support Server-Sent Events; Council, Cowork, and Create keep
their local-server behavior and are not exposed by `/share`.

See [Cloudflare Quick Tunnel limitations](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Phone layouts

The Council, Cowork, Create and artifact viewer pages adapt their layout to
narrow screens. Nova adds viewport and overflow handling to authored HTML
artifacts and asks the artifact authoring tools to produce mobile-responsive
layouts. A fixed-width design can still need its own mobile-specific redesign.
