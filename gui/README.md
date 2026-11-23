# OpenFilmKit desktop app

A window for the kit, for people who won't use a terminal. It opens a roll (or a folder of rolls),
shows each frame, lets you adjust brightness, contrast, warmth, color and turn with a live preview,
and saves the picture. It can also scan the next frame into a roll.

It does nothing the command line can't. The app is a thin Electron window over
`openfilmkit.gui_service`, a Python process it talks to with JSON lines. That process calls the
same functions as `openfilmkit convert` and `openfilmkit scan`, so every picture it saves has the
usual recipe beside it.

## Run it from a checkout

```sh
cd gui
npm install
npm start
```

It finds Python in this order: `OPENFILMKIT_PYTHON`, the Homebrew install
(`/opt/homebrew/opt/openfilmkit/libexec/venv/bin/python`), then `python3`. It uses the kit source
from this checkout.

npm 11 blocks Electron's download script by default. If `npx electron --version` fails, run
`npm install-scripts approve electron && npm rebuild electron`. On Node 26, Electron's own
unpacker can fail silently after the download; unpacking the cached zip from
`~/Library/Caches/electron/` into `node_modules/electron/dist` and writing
`Electron.app/Contents/MacOS/Electron` to `node_modules/electron/path.txt` fixes it.

## What's where

- `src/main.js`: the window, the menu, and the Python back end process.
- `src/preload.js`: the only bridge the page has (no Node in the page).
- `src/index.html`, `app.css`, `app.js`: the page, plain DOM with no framework.
- `fonts/`: Inter 4.001 (rsms/inter) for all text and Damion for one script word on one canister, both
  under the SIL Open Font License, served locally. Each font's license is beside it
  (`Inter-OFL.txt`, `Damion-OFL.txt`) and has to ship with it.
- `../openfilmkit/gui_service.py`: the back end. Tests: `../tests/test_gui_service.py`.
- `test/`: `npm test` (Node 21 or later) checks that every method the page calls is one the back end has.

## Not done yet

- Packaging (a `.app` that carries its own Python and SANE, signed and notarized for macOS).
- The scan screen hasn't been run against a real scanner.
- An axe-core run and a VoiceOver pass.
- Eject on Windows: the export sheet says to use “Safely Remove Hardware” instead.
