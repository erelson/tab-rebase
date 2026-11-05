#!/usr/bin/env python3
"""Native messaging host for Tab Rebase.

Receives a window's tabs from the extension, writes them to a
git-rebase-style todo file, opens it in your editor inside a terminal,
waits for the editor to exit, and sends back the parsed lines.

Firefox launches this without a TTY, so the editor has to run in a new
terminal window. Configure via environment variables or the constants below:

  TAB_REBASE_TERMINAL  terminal command prefix, e.g. "kitty" or "alacritty -e"
  TAB_REBASE_EDITOR    editor command, e.g. "nvim" (falls back to $VISUAL/$EDITOR/vim)
  TAB_REBASE_WSL_EDITOR   Windows only: run this editor inside WSL instead,
                          e.g. "vim" or "nvim"  (setx TAB_REBASE_WSL_EDITOR vim)
  TAB_REBASE_WSL_DISTRO   optional WSL distro name; default distro if unset

Note: Firefox's environment is whatever it was launched with, so a desktop
launcher may not see $EDITOR from your shell rc. Set the constants if needed.

Windows: no terminal is needed; the editor is started in a new console
window (Windows Terminal or the classic console, per your Windows default
terminal setting). If EDITOR isn't set, it looks for nvim/vim on PATH, then
Vim's installer location, then the vim bundled with Git for Windows.
"""

import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --- configuration --------------------------------------------------------
IS_WINDOWS = os.name == "nt"
TERMINAL = os.environ.get("TAB_REBASE_TERMINAL", "")  # "" = auto-detect
# To hard-code, e.g.:  EDITOR = r"C:\Program Files\Vim\vim91\vim.exe"
EDITOR = os.environ.get("TAB_REBASE_EDITOR") or os.environ.get("VISUAL") \
    or os.environ.get("EDITOR") or ""  # "" = auto-detect
# Windows: if set, the editor runs inside WSL (overrides EDITOR), e.g. "vim".
WSL_EDITOR = os.environ.get("TAB_REBASE_WSL_EDITOR", "")
WSL_DISTRO = os.environ.get("TAB_REBASE_WSL_DISTRO", "")
# Ask "Close N tabs? [y/N]" in the editor's console after :wq, whenever
# the edit would close at least one tab. TAB_REBASE_CONFIRM=0 turns it off.
CONFIRM = os.environ.get("TAB_REBASE_CONFIRM", "1") != "0"
CONFIRM_LIST_MAX = 20  # list at most this many of the tabs to be closed
TITLE_WIDTH = 60  # titles are padded to this width so URLs line up
DOMAIN_WIDTH = 15  # domain column shows at most this many characters
# Vim syntax + settings, loaded with -S when the editor is a vim.
VIM_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tabrebase.vim")

# Query parameters stripped before NEAR comparison. Prefix match for
# entries ending in "*".
TRACKING_PARAMS = [
    "utm_*", "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid",
    "yclid", "mc_cid", "mc_eid", "igshid", "si", "ref_src", "ref_url",
    "_hsenc", "_hsmi", "mkt_tok", "oly_anon_id", "oly_enc_id", "vero_id",
]

# Sites that route by #fragment, so the fragment is never ignored for NEAR.
# (Fragments containing / ! or = are kept everywhere anyway.)
HASH_ROUTED_HOSTS = {"mail.google.com", "calendar.google.com"}

# Terminals in auto-detect order, with the args that make them run a
# command and *block until it exits*.
TERMINAL_CANDIDATES = [
    ["kitty"],
    ["alacritty", "-e"],
    ["foot"],
    ["wezterm", "start", "--always-new-process", "--"],
    ["ghostty", "-e"],
    ["gnome-terminal", "--wait", "--"],
    ["konsole", "-e"],
    ["xfce4-terminal", "--disable-server", "-x"],
    ["xterm", "-e"],
]


# --- native messaging protocol -------------------------------------------
def read_message():
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        sys.exit(0)
    (length,) = struct.unpack("@I", raw_len)
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    log(f"sent reply to Firefox ({len(data)} bytes)")


# Each run overwrites this log, next to this script.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tab_rebase_host.log")
_log_file = None
_t0 = time.monotonic()
_in_session = False  # True in the console-side process (see session())


def log(*args):
    global _log_file
    if _in_session:  # don't clobber the host's log or clutter the console
        return
    msg = " ".join(str(a) for a in args)
    # stderr shows up in the Browser Console (Ctrl+Shift+J)
    print("[tab-rebase host]", msg, file=sys.stderr, flush=True)
    try:
        if _log_file is None:
            _log_file = open(LOG_PATH, "w", encoding="utf-8")
        stamp = time.strftime("%H:%M:%S")
        _log_file.write(f"{stamp} +{time.monotonic() - _t0:6.2f}s  {msg}\n")
        _log_file.flush()
    except OSError:
        pass


# --- rendering ------------------------------------------------------------
def flags(t):
    """'#' plus a letter per set flag (e.g. '#AP', '#U'); '' if none."""
    f = ""
    f += "A" if t.get("active") else ""
    f += "P" if t.get("pinned") else ""
    f += "U" if t.get("discarded") else ""   # Unloaded
    f += "S" if t.get("audible") else ""     # playing Sound
    f += "M" if t.get("muted") else ""
    f += "H" if t.get("hidden") else ""
    return "#" + f if f else ""


def domain(url):
    """First DOMAIN_WIDTH chars of the host, never empty or containing spaces.

    about:/file:/etc. have no host, so show the start of the URL instead.
    """
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    d = host or url or "-"
    return "".join(d.split())[:DOMAIN_WIDTH]


def one_line(s):
    return " ".join(s.split())


# --- duplicate detection --------------------------------------------------
def is_tracking(key):
    k = key.lower()
    return any(k.startswith(p[:-1]) if p.endswith("*") else k == p
               for p in TRACKING_PARAMS)


def near_key(url):
    """Loosened URL used for NEAR matching.

    http==https, host lowercased, leading www. and default ports dropped,
    trailing slash dropped, tracking params removed, remaining params
    sorted, #fragment dropped unless it looks like app state (contains
    / ! or =) or the host is in HASH_ROUTED_HOSTS.
    Non-http(s) URLs (about:, file:, moz-extension:) are compared as-is.
    """
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if p.scheme not in ("http", "https"):
        return url
    host = (p.hostname or "").lower()
    keep_frag = host in HASH_ROUTED_HOSTS or any(c in p.fragment for c in "/!=")
    if host.startswith("www."):
        host = host[4:]
    if p.port and p.port not in (80, 443):
        host += f":{p.port}"
    path = p.path.rstrip("/")
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not is_tracking(k)
    ))
    frag = p.fragment if keep_frag else ""
    return urlunsplit(("https", host, path, query, frag))


def pick_keeper(group):
    """Choose which tab in a group of duplicates stays unmarked.

    `group` is a list of tabs. Preference, in order:
      1. pinned
      2. active (the tab you're on)
      3. loaded rather than unloaded/discarded
      4. most recently used
      5. earliest position in the window (tie-break)
    """
    return min(group, key=lambda t: (
        not t.get("pinned"),
        not t.get("active"),
        bool(t.get("discarded")),
        -(t.get("lastAccessed") or 0),
        t["index"],
    ))


def find_duplicates(tabs):
    """Return {tab id: marker string} for every tab that isn't a keeper.

    Exact-URL duplicates get "DUPE <keeper>". Then the surviving tabs
    are grouped by near_key(); non-keepers there get "NEAR <keeper>".
    A tab only ever gets one marker, and DUPE wins.
    """
    def group_by(items, key):
        groups = {}
        for t in items:
            groups.setdefault(key(t), []).append(t)
        return groups.values()

    marks, survivors = {}, []
    for g in group_by(tabs, lambda t: t["url"]):
        keep = pick_keeper(g)
        survivors.append(keep)
        for t in g:
            if t is not keep:
                marks[t["id"]] = f"DUPE {keep['id']}"

    survivors.sort(key=lambda t: t["index"])
    for g in group_by(survivors, lambda t: near_key(t["url"])):
        keep = pick_keeper(g)
        for t in g:
            if t is not keep:
                marks[t["id"]] = f"NEAR {keep['id']}"
    return marks


# --- rendering (cont.) ----------------------------------------------------
def render(msg):
    tabs = msg["tabs"]
    title_w = min(TITLE_WIDTH, max((len(one_line(t["title"])) for t in tabs), default=0))

    def group(t):
        return f"g{t['groupId']}" if t.get("groupId", -1) != -1 else ""

    def width(f):
        return max((len(f(t)) for t in tabs), default=0)

    dom_w = width(lambda t: domain(t["url"]))
    id_w = width(lambda t: str(t["id"]))
    # Optional columns take no space at all when no tab uses them.
    opt = [(f, width(f)) for f in (flags, group) if width(f)]

    marks = find_duplicates(tabs)
    n_dupe = sum(m.startswith("DUPE") for m in marks.values())
    n_near = len(marks) - n_dupe

    lines = []
    for t in tabs:
        cols = [f"{domain(t['url']):<{dom_w}}", f"{t['id']:<{id_w}}"]
        cols += [f"{f(t):<{w}}" for f, w in opt]
        line = (f"p {' '.join(cols)}  {one_line(t['title']):<{title_w}}"
                f"  {t['url']}")
        if t["id"] in marks:
            line += "  " + marks[t["id"]]
        lines.append(line)

    lines += [
        "",
        f"# Tab rebase: window {msg.get('windowId')}, {len(tabs)} tabs, "
        f"{n_dupe} DUPE, {n_near} NEAR",
        "#",
        "# Commands:",
        "# p, pick = keep tab",
        "# d, drop = close tab (deleting the line does the same)",
        ("# Reorder lines to reorder tabs.  :sort /\\>/  sorts by domain."
         if msg.get("reorder") else
         "# Line order is ignored (tabs aren't moved), so :sort /\\>/ etc.\n"
         "# are safe; :sort /\\>/ sorts by domain."),
        "#",
        "# Columns: command, domain (first 15 chars), tab id, flags, title, url",
        "# Flags:   #A=active P=pinned U=unloaded S=playing sound M=muted",
        "#          H=hidden, combined like #AP; gN = in tab group N",
        "#",
        "# Markers (## is the copy that stays unmarked, chosen as: pinned >",
        "# active > loaded > most recently used > earliest in window):",
        "#   DUPE ##  same exact URL as tab ##",
        "#   NEAR ##  same page as tab ## after loosening: http/https, www.,",
        "#            trailing /, #fragment and tracking params ignored",
        "#",
        "# Only the command and id are read back; other edits are ignored.",
        "# :wq applies the changes, asking first if any tabs would close.",
        "# :cq (or quitting without saving) aborts and changes nothing.",
        "# Closed tabs: Ctrl+Shift+T to reopen.",
    ]
    return "\n".join(lines) + "\n"


# --- editor ---------------------------------------------------------------
def terminal_cmd():
    if TERMINAL:
        return shlex.split(TERMINAL)
    for cand in TERMINAL_CANDIDATES:
        if shutil.which(cand[0]):
            return cand
    raise RuntimeError("no terminal found; set TAB_REBASE_TERMINAL")


def split_cmd(s):
    if os.path.isfile(s):  # a bare path, possibly with spaces
        return [s]
    if IS_WINDOWS:  # posix-mode shlex would eat backslashes
        return [p.strip('"') for p in shlex.split(s, posix=False)]
    return shlex.split(s)


def windows_editor_candidates():
    pf = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)",
                                      "ProgramW6432")]
    pf = [p for p in pf if p]
    local = os.environ.get("LOCALAPPDATA", "")
    globs = [os.path.join(p, "Vim", "vim*", "vim.exe") for p in pf]
    globs += [os.path.join(p, "Git", "usr", "bin", "vim.exe") for p in pf]
    if local:
        globs.append(os.path.join(local, "Programs", "Git", "usr", "bin", "vim.exe"))
    import glob
    for g in globs:
        for hit in sorted(glob.glob(g), reverse=True):  # newest vimNN first
            yield hit


def editor_cmd():
    if EDITOR:
        return split_cmd(EDITOR)
    for name in ("nvim", "vim"):
        found = shutil.which(name)
        if found:
            return [found]
    if IS_WINDOWS:
        for hit in windows_editor_candidates():
            return [hit]
    raise RuntimeError("no editor found; set EDITOR in tab_rebase_host.py")


def is_vim(prog):
    """True for vim, nvim, gvim, vim.exe, ... (they all accept -S script)."""
    return "vim" in os.path.basename(prog.replace("\\", "/")).lower()


def with_script(cmd, script):
    """Editor command plus '-S script' if it's a vim and a script exists."""
    if script and is_vim(cmd[0]):
        return cmd + ["-S", script]
    return cmd


def wsl_cmd(path, script=None):
    """wsl.exe command that opens `path` (a Windows path) in WSL_EDITOR.

    --cd takes the Windows directory and WSL translates it, so only plain
    temp-file names cross over (no quoting/backslash issues). `script`
    must be in the same directory as `path`.
    bash -l loads ~/.profile so editors in ~/.local/bin etc. are found.
    """
    editor = split_cmd(WSL_EDITOR)
    if script:
        editor = with_script(editor, os.path.basename(script))
    cmd = ["wsl.exe"]
    if WSL_DISTRO:
        cmd += ["-d", WSL_DISTRO]
    cmd += ["--cd", os.path.dirname(path), "-e", "bash", "-lc",
            "exec " + " ".join(editor + [os.path.basename(path)])]
    return cmd


def inline_editor_cmd(path, script):
    """Editor command to run in the *current* console/terminal."""
    if IS_WINDOWS and WSL_EDITOR:
        return wsl_cmd(path, script)
    return with_script(editor_cmd(), script) + [path]


def sidecar(path, ext):
    return path[:-len(".tabs")] + ext


def edit(path, script=None):
    """Open a console/terminal running session(); return (exit code, declined).

    The console runs this same script with --session, which starts the
    editor in that console and then asks for confirmation there.
    """
    cmd = [sys.executable, os.path.abspath(__file__), "--session", path,
           script or ""]
    log("editor command:", inline_editor_cmd(path, script))
    if IS_WINDOWS and not TERMINAL:
        log("running in new console:", cmd)
        # New console window; it gets its own stdin/stdout, so don't pass
        # ours (those are the Firefox pipe).
        rc = subprocess.run(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE
                            ).returncode
    else:
        cmd = terminal_cmd() + cmd
        log("running in terminal:", cmd)
        # Keep the terminal off our stdin/stdout — those are the Firefox pipe.
        rc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL).returncode
    try:
        with open(sidecar(path, ".result.json"), encoding="utf-8") as f:
            result = json.load(f)
        return result["editorExit"], result["declined"]
    except (OSError, ValueError, KeyError) as e:
        log(f"no session result ({e}); console exit code {rc}")
        return (rc or -1), False


# --- confirmation (runs in the editor's console) ---------------------------
def read_key():
    """One keypress without Enter where possible; '' on EOF/failure."""
    try:
        if IS_WINDOWS:
            import msvcrt
            return msvcrt.getwch()
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:  # not a tty (e.g. tests): fall back to a line
        return sys.stdin.readline()[:1]


def confirm(path, snapshot, reorder_on=False):
    """Show what :wq would do and ask. Returns True to go ahead."""
    with open(path, encoding="utf-8") as f:
        plan, errors = parse(f.read())
    if errors:
        print(f"\n{len(errors)} line(s) couldn't be read, so nothing will "
              "change:")
        for e in errors[:10]:
            print(f"  line {e['line']}: {e['text']}")
        print("\nPress any key.", end="", flush=True)
        read_key()
        return True  # the extension aborts on parse errors anyway

    by_id = {t["id"]: t for t in snapshot}
    kept, seen = [], set()
    for p in plan:
        if p["id"] in by_id and p["id"] not in seen:
            seen.add(p["id"])
            if p["cmd"] == "pick":
                kept.append(p["id"])
    keep_set = set(kept)
    closing = [t for t in snapshot if t["id"] not in keep_set]
    if not closing:
        return True  # reorder only: nothing to confirm
    if not kept:
        print("\nThat would close every tab, so nothing will change.")
        print("Press any key.", end="", flush=True)
        read_key()
        return True  # the extension refuses this case too

    reorder = reorder_on and \
        kept != [t["id"] for t in snapshot if t["id"] in keep_set]
    print(f"\nClose {len(closing)} of {len(snapshot)} tabs"
          f"{' and reorder the rest' if reorder else ''}?\n")
    for t in closing[:CONFIRM_LIST_MAX]:
        title = one_line(t["title"])[:60] or "(no title)"
        print(f"  - {title}  [{domain(t['url'])}]")
    if len(closing) > CONFIRM_LIST_MAX:
        print(f"  ... and {len(closing) - CONFIRM_LIST_MAX} more")
    print("\n[y/N] ", end="", flush=True)
    key = read_key()
    print(key if key.isprintable() else "")
    return key.lower() == "y"


def session(path, script):
    """Console-side process: run the editor here, then confirm here.

    Writes {"editorExit", "declined"} next to the tab file for the host.
    """
    global _in_session
    _in_session = True
    result = {"editorExit": -1, "declined": False}
    try:
        mtime = os.path.getmtime(path)
        result["editorExit"] = subprocess.run(
            inline_editor_cmd(path, script or None)).returncode
        saved = os.path.getmtime(path) != mtime
        if CONFIRM and saved and result["editorExit"] == 0:
            with open(sidecar(path, ".snap.json"), encoding="utf-8") as f:
                snap = json.load(f)
            result["declined"] = not confirm(path, snap["tabs"],
                                             snap["reorder"])
            if result["declined"]:
                print("Cancelled; nothing will change.")
                time.sleep(0.8)
    except Exception:
        import traceback
        traceback.print_exc()
        print("\nPress any key.", end="", flush=True)
        read_key()
    finally:
        with open(sidecar(path, ".result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f)


# --- parsing --------------------------------------------------------------
COMMANDS = {"p": "pick", "pick": "pick", "d": "drop", "drop": "drop"}


def parse(text):
    plan, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # "<cmd> <domain> <id> ..."; also accept "<cmd> <id> ..." for lines
        # typed by hand. Domains always contain a non-digit.
        parts = s.split(None, 3)
        tab_id = next((p for p in parts[1:3] if p.isdigit()), None)
        if parts[0] not in COMMANDS or tab_id is None:
            errors.append({"line": n, "text": line})
            continue
        plan.append({"cmd": COMMANDS[parts[0]], "id": int(tab_id)})
    return plan, errors


# --- main -----------------------------------------------------------------
def main():
    log(f"host started: python {sys.version.split()[0]}, {sys.executable}")
    msg = read_message()
    log(f"received action={msg.get('action')!r}, {len(msg.get('tabs', []))} tabs")
    if msg.get("action") != "edit":
        send_message({"ok": False, "error": f"unknown action {msg.get('action')!r}"})
        return

    fd, path = tempfile.mkstemp(prefix="tab-rebase-", suffix=".tabs")
    script = None
    try:
        # LF line endings, so vim in WSL doesn't treat it as a DOS file.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(msg))
        log("wrote", path)
        # Syntax/settings script goes next to it so vim in WSL can reach it
        # by bare name too.
        if os.path.isfile(VIM_SCRIPT):
            script = path[:-len(".tabs")] + ".vim"
            with open(VIM_SCRIPT, encoding="utf-8") as src, \
                    open(script, "w", encoding="utf-8", newline="\n") as dst:
                dst.write(src.read())
        # Tab list for the confirmation prompt in the console.
        with open(sidecar(path, ".snap.json"), "w", encoding="utf-8") as f:
            json.dump({
                "reorder": bool(msg.get("reorder")),
                "tabs": [{k: t.get(k) for k in ("id", "title", "url")}
                         for t in msg["tabs"]],
            }, f)
        mtime = os.path.getmtime(path)
        started = time.monotonic()
        rc, declined = edit(path, script)
        took = time.monotonic() - started
        saved = os.path.getmtime(path) != mtime
        log(f"editor exited: code={rc}, after {took:.1f}s, saved={saved}, "
            f"declined={declined}")
        if took < 2:
            log("WARNING: editor returned almost immediately; it may have "
                "detached (e.g. a launcher/shim) instead of waiting for :wq")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        plan, errors = parse(text)
        log(f"parsed {len(plan)} lines "
            f"({sum(p['cmd'] == 'drop' for p in plan)} drop), "
            f"{len(errors)} unparseable")
        send_message({
            "ok": True,
            "editorExit": rc,
            "editorSeconds": round(took, 1),
            "saved": saved,
            "declined": declined,
            "plan": plan,
            "errors": errors,
        })
    except Exception as e:
        import traceback
        log("error:", traceback.format_exc())
        send_message({"ok": False, "error": str(e)})
    finally:
        for p in (path, script, sidecar(path, ".snap.json"),
                  sidecar(path, ".result.json")):
            try:
                if p:
                    os.unlink(p)
            except OSError:
                pass


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--session":
        session(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    else:
        main()
