# Authentication & Access Control

FreeSky requires a login for the web UI and a per-user token for the playlist and
stream endpoints. Clients on a whitelisted subnet bypass both.

## Roles

| Role | Web UI | Watch channels | Settings & user management |
|---|---|---|---|
| `admin` | yes | yes | yes |
| `standard` | yes | yes | no |
| trusted subnet (not signed in) | yes | yes | **no** |

Being on a trusted network never grants admin. Managing settings always requires
signing in as an admin.

## First run

On first boot, if no users exist, an admin is created:

- `ADMIN_USER` — username (default `admin`)
- `ADMIN_PASS` — password. **If unset, a random password is generated and printed
  to the container log.** No guessable default ships.

```bash
docker compose logs freesky | grep -A2 "FIRST RUN"
```

Bootstrap only runs when `users.json` is empty, so `ADMIN_PASS` left in the
environment will never silently reset a password changed later in the UI.

## Playlist authentication (Dispatcharr)

Dispatcharr has no username/password or custom-header field for M3U sources — it
simply fetches the URL you give it. So the credential is a per-user token carried
in the URL:

```
http://<host>:3000/playlist.m3u8?token=<user token>
```

Settings → Users → **Playlist URL** copies this per user. The token is propagated
automatically onto every nested URL (`/api/stream/…`, `/api/content/…`,
`/api/key/…`), because the player fetches those separately and sends no cookie.

**Revoking access:** Settings → Users → the rotate button issues a new token and
immediately invalidates the old URL everywhere. Deleting the user does the same.

Protected paths: `/playlist.m3u8`, `/api/stream/*`, `/api/content/*`, `/api/key/*`,
`/epg.xml`. Logo endpoints stay open — they carry no sensitive data and players
fetch them unauthenticated.

## Trusted networks

Settings → **Trusted networks**, or seed with `TRUSTED_NETWORKS` (comma-separated
CIDRs, e.g. `192.168.3.0/24,10.0.0.0/8`). Clients in these ranges reach the app and
the playlist without a login or token. Leave empty to require authentication from
everywhere.

The client IP comes from what Caddy forwards. Caddy does **not** trust a
client-supplied `X-Forwarded-For` — it replaces it with the real peer — so the
whitelist cannot be spoofed by a header.

> **Important:** this holds only while Caddy is the sole ingress. The compose file
> uses `network_mode: host`, which also exposes `BACKEND_PORT` (8005) directly.
> Anything reachable on that port bypasses Caddy and *can* forge `X-Forwarded-For`.
> Firewall 8005 to localhost if the host is internet-facing.

## Password storage

stdlib `hashlib.scrypt` (N=2¹⁵, r=8), 16-byte random salt per user, compared with
`hmac.compare_digest`. No third-party dependency.

Login failures are deliberately indistinguishable: an unknown username still runs
one scrypt against a decoy hash, so a wrong password and a nonexistent account take
the same time and return the same message — `Invalid username or password`. This
prevents account enumeration.

## Files

All under the `./data` volume so they survive redeploys:

| File | Contents |
|---|---|
| `users.json` | usernames, emails, roles, salted hashes, stream tokens |
| `app_settings.json` | trusted network CIDRs |
| `channel_prefs.json` | disabled channel ids |

Relevant env vars: `USERS_FILE`, `APP_SETTINGS_FILE`, `CHANNEL_PREFS_FILE`,
`TRUSTED_NETWORKS`, `ADMIN_USER`, `ADMIN_PASS`.

## Known limitation

Reflex renders a page shell before `on_load` runs, so an unauthenticated visitor
sees a brief flash of empty layout before redirecting to `/login`. Channel data
loads only after the guard passes, so no content leaks. There is no server-side
pre-render block in Reflex 0.9.7.
