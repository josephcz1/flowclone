"""Build FlowClone.app — a double-clickable menu-bar app, no terminal window.

Usage:
    uv run python scripts/build_app.py             # build dist/FlowClone.app
    uv run python scripts/build_app.py --install   # …and copy to /Applications

The bundle is a thin native wrapper: a small compiled launcher that runs this
project's .venv/bin/flowclone as a child and waits. The launcher stays alive as
the responsible process, so macOS attributes the Microphone / Input Monitoring /
Accessibility grants to "FlowClone" instead of a terminal app. Python, MLX and
the model stay where they are — the app just points at this checkout, so a
rebuild is only needed if you move the repo (and re-signing may re-ask the
permission grants).
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP = DIST / "FlowClone.app"
FLOWCLONE_BIN = ROOT / ".venv" / "bin" / "flowclone"
BUNDLE_ID = "com.flowclone.app"

LAUNCHER_C = r"""
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* Runs flowclone as a child and waits: this process is the one LaunchServices
   holds responsible, so TCC permission prompts and grants name FlowClone. */
static pid_t child = 0;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

int main(void) {
    /* Tell the Python child which .app bundle is actually running. The menu's
       Launch at Login toggle uses this exact path instead of assuming the app
       lives in /Applications or launching the Python binary outside its TCC
       identity. */
    char executable[PATH_MAX];
    uint32_t executable_size = sizeof executable;
    if (_NSGetExecutablePath(executable, &executable_size) == 0) {
        char *contents = strstr(executable, "/Contents/MacOS/");
        if (contents != NULL) {
            *contents = '\0';
            setenv("FLOWCLONE_APP_PATH", executable, 1);
        }
    }

    const char *home = getenv("HOME");
    char log[1024];
    snprintf(log, sizeof log, "%s/Library/Logs/FlowClone.log", home ? home : "/tmp");
    int fd = open(log, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); close(fd); }

    if (access(FLOWCLONE_BIN, X_OK) != 0) {
        execl("/usr/bin/osascript", "osascript", "-e",
              "display alert \"FlowClone\" message \"The project environment is "
              "missing. In the flowclone checkout, run: uv sync — then relaunch.\"",
              (char *)NULL);
        return 1;
    }

    child = fork();
    if (child == 0) {
        setenv("PYTHONUNBUFFERED", "1", 1);
        execl(FLOWCLONE_BIN, FLOWCLONE_BIN, (char *)NULL);
        _exit(127);
    }
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""

INFO_PLIST = {
    "CFBundleName": "FlowClone",
    "CFBundleDisplayName": "FlowClone",
    "CFBundleIdentifier": BUNDLE_ID,
    "CFBundleExecutable": "FlowClone",
    "CFBundleIconFile": "FlowClone",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "0.1.0",
    "CFBundleVersion": "1",
    # Menu-bar app: no Dock icon, no app switcher entry.
    "LSUIElement": True,
    "NSMicrophoneUsageDescription": "FlowClone records while you hold Right ⌘ so it can transcribe your dictation on-device.",
}


def draw_icon(master_png: Path) -> None:
    """A thin white mic outline (SF Symbol) on a dark rounded square."""
    from AppKit import (
        NSBezierPath,
        NSBitmapImageRep,
        NSColor,
        NSCompositingOperationSourceIn,
        NSCompositingOperationSourceOver,
        NSDeviceRGBColorSpace,
        NSFontWeightLight,
        NSGraphicsContext,
        NSImage,
        NSImageSymbolConfiguration,
        NSMakeRect,
        NSPNGFileType,
        NSRectFillUsingOperation,
        NSZeroRect,
    )

    size = 1024

    symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic", None)
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        size * 0.5, NSFontWeightLight
    )
    symbol = symbol.imageWithSymbolConfiguration_(cfg) or symbol
    # Recolor to white in its own image: SourceIn repaints only where the glyph
    # has alpha, which would wipe the background if done on the shared canvas.
    glyph = NSImage.alloc().initWithSize_(symbol.size())
    glyph.lockFocus()
    bounds = NSMakeRect(0, 0, symbol.size().width, symbol.size().height)
    symbol.drawInRect_fromRect_operation_fraction_(
        bounds, NSZeroRect, NSCompositingOperationSourceOver, 1.0
    )
    NSColor.whiteColor().set()
    NSRectFillUsingOperation(bounds, NSCompositingOperationSourceIn)
    glyph.unlockFocus()

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)

    # macOS leaves a transparent margin around the squircle; ~10% matches stock icons.
    inset = size * 0.10
    box = NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = (size - 2 * inset) * 0.225
    NSColor.colorWithCalibratedWhite_alpha_(0.11, 1.0).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(box, radius, radius).fill()

    gsize = glyph.size()
    scale = (size * 0.44) / gsize.height
    w, h = gsize.width * scale, gsize.height * scale
    glyph.drawInRect_fromRect_operation_fraction_(
        NSMakeRect((size - w) / 2, (size - h) / 2, w, h),
        NSZeroRect,
        NSCompositingOperationSourceOver,
        1.0,
    )

    NSGraphicsContext.restoreGraphicsState()
    rep.representationUsingType_properties_(NSPNGFileType, None).writeToFile_atomically_(
        str(master_png), True
    )


def build_icns(resources: Path) -> None:
    master = DIST / "icon-master.png"
    draw_icon(master)
    iconset = DIST / "FlowClone.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    for pts in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = pts * scale
            name = f"icon_{pts}x{pts}" + ("@2x" if scale == 2 else "") + ".png"
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(master), "--out", str(iconset / name)],
                check=True,
                capture_output=True,
            )
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "FlowClone.icns")],
        check=True,
    )
    shutil.rmtree(iconset)
    master.unlink()


def main() -> int:
    if not FLOWCLONE_BIN.exists():
        print(f"✗ {FLOWCLONE_BIN} missing — run `uv sync` first.", file=sys.stderr)
        return 1

    shutil.rmtree(APP, ignore_errors=True)
    macos = APP / "Contents" / "MacOS"
    resources = APP / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    with open(APP / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(INFO_PLIST, fh)

    src = DIST / "launcher.c"
    src.write_text(LAUNCHER_C)
    subprocess.run(
        [
            "cc",
            "-O2",
            f'-DFLOWCLONE_BIN="{FLOWCLONE_BIN}"',
            "-o",
            str(macos / "FlowClone"),
            str(src),
        ],
        check=True,
    )
    src.unlink()

    build_icns(resources)

    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(APP)],
        check=True,
    )
    print(f"✓ built {APP}")

    if "--install" in sys.argv:
        target = Path("/Applications/FlowClone.app")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(APP, target, symlinks=True)
        print(f"✓ installed {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
