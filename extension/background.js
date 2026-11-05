// Tab Rebase: edit the current window's tabs in your editor, git-rebase
// style, then apply the result: dropped/deleted lines close tabs. With
// REORDER on, line order also becomes tab order.
//
// Nothing is changed if:
//   - the editor exits non-zero (:cq in vim), like aborting a rebase
//   - the file wasn't saved
//   - you answered no at the "Close N tabs? [y/N]" prompt
//   - any line couldn't be parsed
//   - no tabs would be left (the window would close)
//
// Toolbar badge:
//   "…"    waiting for the editor
//   "-N"   applied, N tabs closed (green)       "✓" applied, nothing closed
//   "×"    aborted, nothing changed (orange)    "ERR" failed (red)
// Details: Firefox about:debugging -> Tab Rebase -> Inspect -> Console;
// Chrome chrome://extensions -> Tab Rebase -> "service worker" link.
// Closed tabs can be reopened with Ctrl+Shift+T / History > Recently Closed.
//
// This one file runs in both Firefox (MV2 background page, `browser.*`)
// and Chrome (MV3 service worker, promise-based `chrome.*`).

const api = typeof browser !== "undefined" ? browser : chrome;
const action = api.action || api.browserAction; // MV3 : MV2

const HOST = "tab_rebase";
const BADGE_CLEAR_MS = 20000;
// false: only close tabs; line order in the file is ignored, so :sort etc.
// are just for viewing. true: also move tabs to match the line order.
const REORDER = false;
const log = (...a) => console.log("[tab-rebase]", ...a);
const warn = (...a) => console.warn("[tab-rebase]", ...a);

function tabInfo(t) {
  return {
    id: t.id,
    index: t.index,
    title: t.title || "",
    url: t.url || "",
    active: t.active,
    pinned: t.pinned,
    discarded: t.discarded,
    audible: !!t.audible,
    muted: !!(t.mutedInfo && t.mutedInfo.muted),
    hidden: !!t.hidden, // Firefox only
    lastAccessed: t.lastAccessed || null,
    // Present where tab groups exist; -1 means ungrouped.
    groupId: typeof t.groupId === "number" ? t.groupId : -1,
  };
}

let badgeTimer = null;
function badge(text, color, autoClear = false) {
  clearTimeout(badgeTimer);
  action.setBadgeText({ text });
  if (color) action.setBadgeBackgroundColor({ color });
  if (autoClear) {
    badgeTimer = setTimeout(() => action.setBadgeText({ text: "" }), BADGE_CLEAR_MS);
  }
}

// One request/reply with the native host over a port. A port (rather than
// sendNativeMessage) keeps Chrome's service worker alive while the editor
// is open, however long that takes.
function nativeRequest(msg) {
  return new Promise((resolve, reject) => {
    const port = api.runtime.connectNative(HOST);
    let replied = false;
    port.onMessage.addListener((reply) => {
      replied = true;
      resolve(reply);
      port.disconnect();
    });
    port.onDisconnect.addListener((p) => {
      if (replied) return;
      const err = (p && p.error) || api.runtime.lastError; // Firefox : Chrome
      reject(new Error((err && err.message) || "native host exited without replying"));
    });
    port.postMessage(msg);
  });
}

class Abort extends Error {}

// Turn the host's reply into concrete actions and carry them out.
// `snapshot` is the tab list that was sent to the editor.
async function applyPlan(windowId, snapshot, reply) {
  if (reply.editorExit !== 0) {
    throw new Abort(`editor exited with code ${reply.editorExit} (:cq?)`);
  }
  if (!reply.saved) throw new Abort("file wasn't saved");
  if (reply.declined) throw new Abort("cancelled at the confirmation prompt");
  if (reply.errors.length) {
    warn("unparseable lines:", reply.errors);
    throw new Abort(
      `${reply.errors.length} line(s) couldn't be parsed, e.g. line ` +
        `${reply.errors[0].line}: ${reply.errors[0].text}`
    );
  }

  // --- resolve the plan against the snapshot ------------------------------
  const snapById = new Map(snapshot.map((t) => [t.id, t]));
  const seen = new Set();
  const order = []; // kept tab ids, in file order
  const drop = new Set();
  for (const { cmd, id } of reply.plan) {
    if (!snapById.has(id)) {
      warn(`tab ${id} wasn't in this window's list; line ignored`);
      continue;
    }
    if (seen.has(id)) {
      warn(`tab ${id} is listed more than once; using its first line`);
      continue;
    }
    seen.add(id);
    if (cmd === "drop") drop.add(id);
    else order.push(id);
  }
  for (const t of snapshot) if (!seen.has(t.id)) drop.add(t.id); // deleted lines

  // --- compare with the window as it is now --------------------------------
  const now = await api.tabs.query({ windowId });
  if (!now.length) throw new Abort("the window is gone");
  const nowById = new Map(now.map((t) => [t.id, t]));
  const gone = snapshot.filter((t) => !nowById.has(t.id));
  if (gone.length) {
    log(`${gone.length} tab(s) were closed while editing; skipping them`);
  }
  const added = now.filter((t) => !snapById.has(t.id));
  if (added.length) {
    log(`${added.length} tab(s) opened while editing; left alone`);
  }

  const toClose = [...drop].filter((id) => nowById.has(id));
  let keep = order.filter((id) => nowById.has(id));
  if (!keep.length && !added.length) {
    throw new Abort("no tabs would be left, so the window would close");
  }

  if (REORDER) {
    // Firefox keeps pinned tabs before unpinned ones.
    const pinned = keep.filter((id) => nowById.get(id).pinned);
    const unpinned = keep.filter((id) => !nowById.get(id).pinned);
    if (keep.some((id, i) => (i < pinned.length) !== nowById.get(id).pinned)) {
      log("pinned tabs were kept ahead of unpinned ones");
    }
    keep = [...pinned, ...unpinned];
  }

  // --- close ---------------------------------------------------------------
  // If the active tab is being closed, switch to the nearest kept tab that
  // came after it (else before it), rather than leaving it to chance.
  const active = now.find((t) => t.active);
  if (active && toClose.includes(active.id) && keep.length) {
    const byPos = keep.map((id) => nowById.get(id)).sort((a, b) => a.index - b.index);
    const next =
      byPos.find((t) => t.index > active.index) || byPos[byPos.length - 1];
    await api.tabs.update(next.id, { active: true });
  }
  if (toClose.length) {
    const closed = toClose.map((id) => nowById.get(id));
    log(`closing ${closed.length} tab(s):`);
    console.table(closed.map((t) => ({ id: t.id, title: t.title, url: t.url })));
    await api.tabs.remove(toClose);
  }

  if (!REORDER) return { closed: toClose.length, moved: 0 };

  // --- reorder -------------------------------------------------------------
  // Put kept tabs at positions 0..n-1 in file order, moving only tabs that
  // aren't already where they belong. Tabs opened meanwhile end up after.
  let cur = (await api.tabs.query({ windowId }))
    .sort((a, b) => a.index - b.index)
    .map((t) => t.id);
  let moved = 0;
  for (let i = 0; i < keep.length; i++) {
    if (cur[i] === keep[i]) continue;
    try {
      await api.tabs.move(keep[i], { index: i });
      moved++;
    } catch (e) {
      warn(`couldn't move tab ${keep[i]}:`, e.message || e);
    }
    cur = (await api.tabs.query({ windowId }))
      .sort((a, b) => a.index - b.index)
      .map((t) => t.id);
  }

  return { closed: toClose.length, moved };
}

let busy = false;

async function rebaseWindow(windowId) {
  if (busy) {
    log("already waiting on an editor; ignoring click");
    return;
  }
  busy = true;
  badge("…", "#666");
  try {
    const tabs = await api.tabs.query({ windowId });
    const snapshot = tabs.map(tabInfo);
    log(`sending ${tabs.length} tabs from window ${windowId} to native host`);
    const reply = await nativeRequest({
      action: "edit",
      reorder: REORDER, // so the help text and prompt describe it correctly
      windowId,
      now: Date.now(),
      tabs: snapshot,
    });
    log("reply received:", JSON.stringify(reply));
    if (!reply || !reply.ok) {
      throw new Error(`host reported an error: ${reply && reply.error}`);
    }

    const { closed, moved } = await applyPlan(windowId, snapshot, reply);
    log(`done: closed ${closed}, moved ${moved}`);
    badge(closed ? `-${closed}` : "✓", "#2a7", true);
  } catch (e) {
    if (e instanceof Abort) {
      log(`aborted, nothing changed: ${e.message}`);
      badge("×", "#d80", true);
    } else {
      console.error("[tab-rebase] failed:", e);
      badge("ERR", "#c00");
    }
  } finally {
    busy = false;
  }
}

action.onClicked.addListener((tab) => {
  log("toolbar button clicked");
  rebaseWindow(tab.windowId);
});

log("background script loaded");
