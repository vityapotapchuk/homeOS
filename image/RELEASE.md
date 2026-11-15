SD card images of HomeOS, built from the latest Raspberry Pi OS Lite (64-bit).

| File | For | What is in it |
|---|---|---|
| `homeos-zero2w-*.img.xz` | Raspberry Pi Zero 2 W | Lite edition: HomeOS web access (start screen, menu, settings, Wi-Fi, Bluetooth, terminal, Files, Monitor, Radio, updates). No browser, no camera, no desktop. |
| `homeos-pi5-*.img.xz` | Raspberry Pi 5 | Full edition: HomeOS desktop on the screen, web access, the Pi browser (Chromium), Camera and motion detection. |

**Writing the card**
1. Raspberry Pi Imager → Choose OS → *Use custom* → pick the `.img.xz` file.
2. When Imager offers OS customisation, set a user name and password, Wi-Fi and SSH.
3. After writing, open the `bootfs` drive and fill in `homeos.txt`: the web password and,
   if you use Tailscale, an auth key. (No Imager customisation? Fill in the user and Wi-Fi there too.)
4. Put the card in the Pi. The first start takes a few minutes longer while HomeOS sets itself up;
   the Pi 5 then restarts into the HomeOS desktop.

Web access: `http://homeos.local:8790` on your home network, or the Pi's Tailscale address when a key was given.
The Radio app plays any HTTP audio stream; set its address in Settings > Apps.
