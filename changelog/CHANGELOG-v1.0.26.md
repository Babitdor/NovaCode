# NovaCode v1.0.26

- `/remote` now includes Telegram user authorization controls: a user ID field, Authorize/Revoke buttons, and the saved authorized user list. The panel scrolls on shorter terminals to keep the controls accessible.
- `/remote` subcommands entered in the prompt now execute directly, including during an active task, instead of opening the remote panel and discarding arguments.
- Remote subcommands use the root session's bridge manager even when a child tab is visible.
