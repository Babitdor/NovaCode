# NovaCode v1.0.60

## Fixed

- Temporary sharing works with current ngrok agents by avoiding the removed
  `--web-addr` flag. Nova disables ngrok's local traffic inspector for its
  managed tunnels, so multiple shares do not compete for its default port.

## Verification

- Added a regression test for the ngrok startup arguments and URL discovery.
