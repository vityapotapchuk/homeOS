# Contributing to HomeOS

Thank you for helping! Bug reports, ideas and pull requests are all welcome.

## Reporting a bug

Open an [issue](https://github.com/vityapotapchuk/HomeOS/issues/new/choose) and include:

- the HomeOS version (Settings → About) and your Pi model;
- whether you use the Pi's screen, web access, or both, and which browser;
- what you did, what you expected and what happened;
- the log if a service fails: `journalctl -u homeos-web -n 100` or `journalctl -u homeos-shell -n 100`.

Security problems: please do **not** open a public issue — see [SECURITY.md](SECURITY.md).

## Working on the code

There is nothing to build. To try a change on a Pi:

```sh
git clone https://github.com/vityapotapchuk/HomeOS.git ~/homeos-src
cd ~/homeos-src
sudo ./install.sh --web --no-packages   # copies the files to /opt/homeos and restarts the services
```

Guidelines:

- **Python:** standard library only, Python 3.11+. No new dependencies without a good reason.
- **Interface:** plain HTML, CSS and JavaScript in `shell/ui/`; no frameworks, no build step.
  Keep it working on a phone, an upright tablet, a large landscape screen and an 800×480 Pi display.
- **Security:** anything reachable from the web goes through the session and CSRF checks in
  `shell/webshell.py`; never send secrets (passwords, access codes) back to the browser.
- **Motion:** respect `prefers-reduced-motion` for new animations.
- Add a test in `tests/` for anything that guards files, sign-in or updates.
- Run the same checks as CI before you push:

  ```sh
  python3 -m unittest discover -s tests -t .
  python3 -m py_compile shell/*.py
  node --check shell/ui/*.js
  bash -n install.sh uninstall.sh image/*.sh && for f in bin/*; do sh -n "$f"; done
  ```

## Pull requests

Keep a pull request to one change, describe what it does and how you tested it, and add
a screenshot for visual changes. By contributing you agree that your work is released under
the [MIT licence](LICENSE).
