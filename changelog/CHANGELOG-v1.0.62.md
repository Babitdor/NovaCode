# NovaCode v1.0.62

## Fixed

- Temporary sharing checks that each local page accepts connections before
  Nova starts a public tunnel, and reports the exact local address if it does
  not. This avoids publishing a link that immediately returns ngrok 8012.
