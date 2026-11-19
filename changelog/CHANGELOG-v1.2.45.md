# NovaCode 1.2.45

## Startup fix

- Fixed an agent startup crash during `image_gen` registration. Nova now
  recognizes both plain Python functions and named LangChain tools when
  checking whether `image_gen` is already registered.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.45
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.45"
```
