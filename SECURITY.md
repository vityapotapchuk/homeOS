# Security policy

HomeOS gives web access to a Raspberry Pi, including a terminal, so security reports are
taken seriously.

## Reporting a vulnerability

Please report privately through GitHub:
**[Report a vulnerability](https://github.com/vityapotapchuk/HomeOS/security/advisories/new)**
(Security tab → *Report a vulnerability*). Do not open a public issue.

Include the HomeOS version, what an attacker could do, and steps to reproduce. You will get
an answer within a week; fixes are released as a new version and noted in the release.

## Supported versions

Only the latest release is supported. Update from Settings → Updates.

## Design notes

- The web service listens on `127.0.0.1` and the Pi's Tailscale address only.
- Passwords are stored as scrypt hashes; sessions are HttpOnly cookies with a CSRF token;
  sign-in is rate-limited.
- The printer access code and other secrets are kept in files readable by the owner only and
  are never sent to the browser.
- Passwordless sudo is limited to `/opt/homeos/bin/homeos-update`.
