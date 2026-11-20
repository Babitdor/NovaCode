# NovaCode 1.2.52

## Providers

- A key saved through `/auth` now reaches Google and Anthropic. Both clients
  were built without one: Google resolved the key and then never passed it, and
  Anthropic relied on `ANTHROPIC_API_KEY` being in the environment, which it is
  not for a key held in the system keychain. Google failed with "API key
  required for Gemini Developer API"; Anthropic failed with "Could not resolve
  authentication method".
- A 429 that merely mentions "quota" is no longer treated as an exhausted
  account. Google, Azure and several gateways word per-minute throttling as an
  exceeded quota, so those requests were reported as "usage limit reached",
  given upgrade advice, and never retried. A 429 now counts as a cap only when
  it names one (`insufficient_quota`, "usage limit", a weekly or monthly limit,
  a credit balance, or an allowance); otherwise it is a rate limit that backs
  off and retries. Command Code's `upgrade_required` keeps its own "no API
  access" notice instead of being read as a usage cap.

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
