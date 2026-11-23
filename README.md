# Tab Rebase

Clean up a browser window's tabs in vim, git-rebase style. Click the toolbar
button (or Alt+Shift+E), the window's tabs open as a text file in your editor,
mark tabs `d` or delete their lines, `:wq`, confirm, and those tabs close.

```
p www.reddit.com  12      r/vim - nowrap tips    https://www.reddit.com/r/vim/...
p github.com      20 #P   GitHub foo/bar         https://github.com/foo/bar
d github.com      21 #U   GitHub foo/bar         https://github.com/foo/bar  DUPE 20
```

Works in Firefox and Chrome (also Chromium and Edge).

## Layout

| Path | What |
|---|---|
| `extension/background.js` | The extension; one file shared by both browsers |
| `extension/manifest.firefox.json` | Firefox manifest (MV2) |
| `extension/manifest.chrome.json` | Chrome manifest (MV3); its `key` fixes the extension ID |
| `build.py` | Assembles `dist/firefox/` and `dist/chrome/` from `extension/` |
| `dist/` | Ready-to-load extension folders (generated; re-run `build.py` after edits) |
| `host/tab_rebase_host.py` | Native host: writes the tab list, runs the editor, asks to confirm, parses the result |
| `host/tabrebase.vim` | Vim highlighting/settings for the tab list |
| `host/install.py` | Registers the host with Firefox, Chrome, Chromium and Edge |

## Setup

Needs Python 3 and vim or Neovim (on Windows: native, Git for Windows' vim,
or vim inside WSL).

1. **Register the host** (once, and again if you move the folder):
   `py host\install.py` on Windows, `python3 host/install.py` elsewhere.
2. **Build the extension:** `py build.py` on Windows, `python3 build.py`
   elsewhere. This creates `dist/firefox/` and `dist/chrome/`. Re-run it
   after editing anything in `extension/`, then reload the extension.
3. **Load the extension**
   - **Firefox:** `about:debugging#/runtime/this-firefox` → *Load Temporary
     Add-on…* → `dist/firefox/manifest.json`. Temporary add-ons are removed on
     restart; for a permanent install sign it as unlisted on AMO
     (`npx web-ext sign --channel=unlisted` in `dist/firefox`) or use
     Developer Edition with `xpinstall.signatures.required = false`.
   - **Chrome / Edge:** `chrome://extensions` (or `edge://extensions`) →
     enable *Developer mode* → *Load unpacked* → `dist/chrome`. Stays installed
     across restarts.
4. Click the toolbar button (it may be under the puzzle-piece menu).

## Configuration

Environment variables (or the constants at the top of `tab_rebase_host.py`):

| Variable | Effect |
|---|---|
| `TAB_REBASE_EDITOR` | Editor command (default: auto-detect nvim/vim) |
| `TAB_REBASE_WSL_EDITOR` | Windows: run this editor inside WSL instead, e.g. `vim` |
| `TAB_REBASE_WSL_DISTRO` | WSL distro to use (default distro if unset) |
| `TAB_REBASE_TERMINAL` | Linux/macOS terminal command (default: auto-detect) |
| `TAB_REBASE_CONFIRM` | `0` to skip the "Close N tabs?" prompt |

Browsers only see environment variables present when they start, so restart
the browser after `setx`. `REORDER` at the top of `extension/background.js`
turns on reordering tabs to match line order (off: line order is ignored).

## Debugging

- `host/tab_rebase_host.log`: each step of the last run.
- Extension console: Firefox `about:debugging` → Tab Rebase → *Inspect*;
  Chrome `chrome://extensions` → Tab Rebase → *service worker*.
- Toolbar badge: `…` editing, `-N` closed N tabs, `✓` nothing closed,
  `×` cancelled (nothing changed), `ERR` failed.
