# NovaCode v1.0.63

## Added

- Artifacts are automatically hosted with free Cloudflare Quick Tunnels, with
  ngrok as a fallback. Council, Cowork, and Create retain their existing local
  server behavior; manual `/share` remains available for those pages. Artifact
  hosting resumes with a restored session, while `/clear` revokes that
  session's hosted artifact page.
