// Token handling for daemons bound beyond loopback (maestro-daemon --bind).
// Open the console as http://host:port/?token=<t> — the token is captured into
// sessionStorage, stripped from the address bar, and then sent on every API
// call (Authorization header; query param for EventSource, which cannot set
// headers). Loopback daemons need no token at all.

const KEY = "maestro_token";

export function initToken() {
  const url = new URL(window.location.href);
  const given = url.searchParams.get("token");
  if (given) {
    sessionStorage.setItem(KEY, given);
    url.searchParams.delete("token");
    window.history.replaceState({}, "", url.toString());
  }
  return sessionStorage.getItem(KEY) || null;
}

export function authHeaders() {
  const token = sessionStorage.getItem(KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function withToken(path) {
  const token = sessionStorage.getItem(KEY);
  return token ? `${path}?token=${encodeURIComponent(token)}` : path;
}
