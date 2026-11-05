"""Register the Tab Rebase native messaging host with Firefox and Chrome.

Run once from this folder:
    Windows:      py install.py
    Linux/macOS:  python3 install.py
Re-run it if you move the folder, reinstall Python, or change the Chrome key.

Writes two host manifests next to this file (Firefox and Chromium-family
browsers use different formats) and registers them:
  Windows: HKCU registry keys for Firefox, Chrome, Chromium and Edge
  Linux:   ~/.mozilla, ~/.config/{google-chrome,chromium,microsoft-edge}
  macOS:   ~/Library/Application Support/{Mozilla,Google/Chrome,Chromium,Microsoft Edge}
No admin rights needed.
"""

import base64
import hashlib
import json
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOST_PY = os.path.join(HERE, "tab_rebase_host.py")
CHROME_MANIFEST = os.path.join(HERE, "..", "extension", "manifest.chrome.json")
FIREFOX_MANIFEST = os.path.join(HERE, "..", "extension", "manifest.firefox.json")
NAME = "tab_rebase"


def firefox_extension_id():
    """The add-on ID, read from the Firefox manifest so they always match."""
    with open(FIREFOX_MANIFEST, encoding="utf-8") as f:
        return json.load(f)["browser_specific_settings"]["gecko"]["id"]


def chrome_extension_id():
    """Chrome derives an unpacked extension's ID from its manifest "key"."""
    with open(CHROME_MANIFEST, encoding="utf-8") as f:
        der = base64.b64decode(json.load(f)["key"])
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def host_path():
    """What the browser launches. Windows can't launch .py, so use a .bat."""
    if os.name != "nt":
        os.chmod(HOST_PY, os.stat(HOST_PY).st_mode | stat.S_IXUSR)
        return HOST_PY
    python = sys.executable
    if python.lower().endswith("pythonw.exe"):
        python = python[:-len("pythonw.exe")] + "python.exe"
    bat = os.path.join(HERE, "tab_rebase_host.bat")
    with open(bat, "w", newline="\r\n") as f:
        f.write("@echo off\n")
        f.write(f'"{python}" -u "{HOST_PY}" %*\n')
    return bat


def write_manifests(path, firefox_id, chrome_id):
    base = {"name": NAME, "description": "Tab Rebase editor bridge",
            "path": path, "type": "stdio"}
    firefox = os.path.join(HERE, "tab_rebase.json")
    chrome = os.path.join(HERE, "tab_rebase_chrome.json")
    with open(firefox, "w", encoding="utf-8") as f:
        json.dump({**base, "allowed_extensions": [firefox_id]}, f, indent=2)
    with open(chrome, "w", encoding="utf-8") as f:
        json.dump({**base, "allowed_origins": [f"chrome-extension://{chrome_id}/"]},
                  f, indent=2)
    return firefox, chrome


def register_windows(firefox, chrome):
    import winreg
    keys = {
        "Firefox": (r"Software\Mozilla\NativeMessagingHosts", firefox),
        "Chrome": (r"Software\Google\Chrome\NativeMessagingHosts", chrome),
        "Chromium": (r"Software\Chromium\NativeMessagingHosts", chrome),
        "Edge": (r"Software\Microsoft\Edge\NativeMessagingHosts", chrome),
    }
    for browser, (parent, manifest) in keys.items():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{parent}\{NAME}") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, manifest)
        print(f"  {browser:9} HKCU\\{parent}\\{NAME}")


def register_files(firefox, chrome):
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        sup = os.path.join(home, "Library", "Application Support")
        dirs = {
            "Firefox": (os.path.join(sup, "Mozilla", "NativeMessagingHosts"), firefox),
            "Chrome": (os.path.join(sup, "Google", "Chrome", "NativeMessagingHosts"), chrome),
            "Chromium": (os.path.join(sup, "Chromium", "NativeMessagingHosts"), chrome),
            "Edge": (os.path.join(sup, "Microsoft Edge", "NativeMessagingHosts"), chrome),
        }
    else:
        cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        dirs = {
            "Firefox": (os.path.join(home, ".mozilla", "native-messaging-hosts"), firefox),
            "Chrome": (os.path.join(cfg, "google-chrome", "NativeMessagingHosts"), chrome),
            "Chromium": (os.path.join(cfg, "chromium", "NativeMessagingHosts"), chrome),
            "Edge": (os.path.join(cfg, "microsoft-edge", "NativeMessagingHosts"), chrome),
        }
    for browser, (d, manifest) in dirs.items():
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, f"{NAME}.json")
        with open(manifest, encoding="utf-8") as src, \
                open(dest, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print(f"  {browser:9} {dest}")


def main():
    firefox_id = firefox_extension_id()
    chrome_id = chrome_extension_id()
    path = host_path()
    firefox, chrome = write_manifests(path, firefox_id, chrome_id)
    print("Host:      ", path)
    print("Firefox ID:", firefox_id)
    print("Chrome ID: ", chrome_id)
    print("Registered for:")
    if os.name == "nt":
        register_windows(firefox, chrome)
    else:
        register_files(firefox, chrome)

    sys.path.insert(0, HERE)
    try:
        import tab_rebase_host as h
        if h.IS_WINDOWS and h.WSL_EDITOR:
            print("Editor:     (WSL)", h.wsl_cmd(r"C:\Temp\tab-rebase-example.tabs"))
        else:
            print("Editor:    ", h.editor_cmd())
    except Exception as e:
        print("Editor:     NOT FOUND -", e)
        print('            Set EDITOR = r"C:\\path\\to\\vim.exe" near the top of '
              "tab_rebase_host.py")


if __name__ == "__main__":
    main()
