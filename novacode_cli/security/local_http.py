"""Browser-origin protection for loopback Nova editing services."""

from http.server import BaseHTTPRequestHandler
import secrets
from urllib.parse import parse_qs, urlsplit


def secure_html(html: str) -> str:
    """Give an authorized page same-origin API access without referrer leaks."""
    bootstrap = """<meta name="referrer" content="no-referrer"><script>
const novaToken = new URLSearchParams(location.search).get('token');
const novaFetch = window.fetch.bind(window);
window.fetch = (url, opts = {}) => {
  const headers = new Headers(opts.headers || {});
  if (new URL(url, location.href).origin === location.origin) headers.set('X-Nova-Token', novaToken);
  return novaFetch(url, {...opts, headers});
};
</script>"""
    return html.replace("<head>", "<head>" + bootstrap, 1)


class LocalRequestHandler(BaseHTTPRequestHandler):
    """Reject foreign origins, DNS rebinding and simple cross-origin mutations."""

    def parse_request(self):
        if not super().parse_request():
            return False
        authority = self.headers.get("Host", "")
        allowed = {
            f"localhost:{self.server.server_port}",
            f"127.0.0.1:{self.server.server_port}",
            f"[::1]:{self.server.server_port}",
        }
        origin = self.headers.get("Origin")
        if authority not in allowed or (origin and origin != f"http://{authority}"):
            self.send_error(403, "Local origin required")
            return False
        parsed = urlsplit(self.path)
        expected = getattr(self.server, "auth_token", "")
        token = self.headers.get("X-Nova-Token", "")
        if self.command == "GET" and parsed.path == "/":
            token = parse_qs(parsed.query).get("token", [token])[0]
        if not expected or not secrets.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        ):
            self.send_error(403, "Nova authorization required")
            return False
        self.path = parsed.path
        if self.command not in {"GET", "HEAD", "OPTIONS"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400, "Invalid request length")
                return False
            if not 0 <= length <= 2_000_000:
                self.send_error(413, "Request body too large")
                return False
        if (
            self.command not in {"GET", "HEAD", "OPTIONS"}
            and self.headers.get("X-Nova-Request") != "1"
        ):
            self.send_error(403, "Nova request header required")
            return False
        return True
