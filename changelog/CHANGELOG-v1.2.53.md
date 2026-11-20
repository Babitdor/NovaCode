# NovaCode 1.2.53

## Credentials

- A locked, missing or broken OS keychain no longer reads as "`ANTHROPIC_API_KEY`
  is not set or is empty" in `/auth`. The status now says the credential store
  could not be read and what to do about it, instead of blaming a key you may
  already have stored. A key that is genuinely absent still gets the precise
  "not set or is empty" message, and a key set in your shell still wins over an
  unreadable keychain.

## Reliability

- The auth, model and provider paths are now pinned by tests where they used to
  be dark: a failing keychain read/write/delete, a rejected save followed by a
  failed session export, a corrupt config, an unstarted keychain reader, a
  refused voice-key migration, provider status for limited or under-configured
  providers, error classification (including a property-based layer), the
  managed GitLab/watsonx/SAP credential-transport guards, endpoint and provider
  resolution, and the reasoning-effort mapping for every provider family.
- Coverage of the credential store, keychain reader, provider auth, provider
  transports and provider-error classifier is 100%; model construction is 83%.

## Install or upgrade

Close NovaCode before upgrading from your shell.

```text
uv tool upgrade novacode-cli
```

For a fresh installation, run `uv tool install --python 3.11 novacode-cli`.
Include any optional extras you use.

```text
uv tool install --python 3.11 "novacode-cli[browser,voice]"
```
