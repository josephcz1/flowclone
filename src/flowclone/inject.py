"""Deliver text to the frontmost app: clipboard set → synthetic ⌘V → restore.

Paste is the only insertion method that works across native apps, Electron,
browsers, and terminals. The previous clipboard contents (all types, including
images/files) are snapshotted and restored shortly after the paste settles.

Posting events requires Accessibility permission for the hosting app
(System Settings → Privacy & Security → Accessibility).
"""

import ctypes
import threading

import Quartz
from AppKit import NSBeep, NSPasteboard, NSPasteboardItem, NSPasteboardTypeString

V_KEYCODE = 9  # kVK_ANSI_V
SYNTHETIC_MARK = 0xF10C  # tags our own events so the hotkey tap ignores them
PASTE_SETTLE_SECONDS = 0.3  # target app must read the pasteboard before restore
# Clipboard managers honoring the nspasteboard.org convention skip this type,
# so dictated text doesn't pollute clipboard history.
TRANSIENT_TYPE = "org.nspasteboard.TransientType"

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")


def secure_input_active() -> bool:
    """True when a password field owns the keyboard; synthetic paste is futile."""
    return bool(_carbon.IsSecureEventInputEnabled())


def can_post_events() -> bool:
    return bool(Quartz.CGPreflightPostEventAccess())


def request_post_event_access() -> bool:
    """Triggers the system Accessibility prompt for the hosting app."""
    return bool(Quartz.CGRequestPostEventAccess())


def _snapshot_pasteboard(pb) -> list[dict]:
    items = []
    for item in pb.pasteboardItems() or []:
        data = {}
        for t in item.types():
            d = item.dataForType_(t)
            if d is not None:
                data[t] = d
        items.append(data)
    return items


def _restore_pasteboard(pb, snapshot, expected_change_count: int | None = None) -> None:
    # If something wrote to the clipboard after our paste (the user copied
    # during the settle window), its changeCount moved on — leave their copy
    # alone rather than clobbering it with the stale snapshot.
    if expected_change_count is not None and pb.changeCount() != expected_change_count:
        return
    try:
        pb.clearContents()
        if not snapshot:
            return
        new_items = []
        for data in snapshot:
            item = NSPasteboardItem.alloc().init()
            for t, d in data.items():
                item.setData_forType_(d, t)
            new_items.append(item)
        pb.writeObjects_(new_items)
    except Exception:
        pass  # losing the old clipboard beats crashing mid-dictation


def _post_cmd_v() -> None:
    for is_down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, V_KEYCODE, is_down)
        Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventSetIntegerValueField(
            event, Quartz.kCGEventSourceUserData, SYNTHETIC_MARK
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def paste_text(text: str) -> bool:
    """Paste text at the frontmost app's cursor. False if skipped (secure input)."""
    if secure_input_active():
        NSBeep()
        return False
    pb = NSPasteboard.generalPasteboard()
    snapshot = _snapshot_pasteboard(pb)
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    pb.setString_forType_("", TRANSIENT_TYPE)
    change_count = pb.changeCount()
    _post_cmd_v()
    timer = threading.Timer(
        PASTE_SETTLE_SECONDS, _restore_pasteboard, args=(pb, snapshot, change_count)
    )
    timer.daemon = True
    timer.start()
    return True


def clipboard_roundtrip_test() -> bool:
    """Save → overwrite → verify → restore, without posting any events."""
    pb = NSPasteboard.generalPasteboard()
    snapshot = _snapshot_pasteboard(pb)
    pb.clearContents()
    pb.setString_forType_("flowclone-clipboard-test", NSPasteboardTypeString)
    ok = pb.stringForType_(NSPasteboardTypeString) == "flowclone-clipboard-test"
    _restore_pasteboard(pb, snapshot)
    return ok
