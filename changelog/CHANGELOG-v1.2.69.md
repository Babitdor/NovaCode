# NovaCode 1.2.69

## Agent workers

- Worker approval requests and progress reach the parent turn even after the parent pauses and resumes for its own approval. This fixes a stall that could occur when Serena delegated to Nova.
- Nova, Nora, and Serena reject attempts to summon their own profile before creating a worker. Ralph can still receive delegated tasks.

## Install or upgrade

```sh
uv tool install --python 3.11 --force "novacode-cli==1.2.69"
```

To include browser, voice, server, Bedrock, and computer-use extras, use the command below.

```sh
uv tool install --python 3.11 --force "novacode-cli[browser,voice,agents-server,server,bedrock,computer]==1.2.69"
```
