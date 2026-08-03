"""Native macOS Preferences window for FlowClone's user-facing settings."""

import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelBorder,
    NSButton,
    NSColor,
    NSControlStateValueOn,
    NSFont,
    NSMakeRect,
    NSPopUpButton,
    NSScrollView,
    NSSlider,
    NSSwitchButton,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from flowclone import config

WINDOW_WIDTH = 560.0
WINDOW_HEIGHT = 610.0


class PreferencesWindow(NSObject):
    def init(self):
        instance = objc.super(PreferencesWindow, self).init()
        if instance is None:
            return None
        instance._on_save = lambda _restart: None
        instance._loaded_quantization = "8bit"
        instance._build_window()
        return instance

    @objc.python_method
    def _build_window(self) -> None:
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("FlowClone Preferences")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        content = self.window.contentView()

        self._section(content, "Transcription", 570)
        self._label(content, "Model precision", 22, 536, 150)
        self.quantization = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(178, 530, 145, 28), False
        )
        self.quantization.addItemsWithTitles_(["8bit", "4bit", "none"])
        content.addSubview_(self.quantization)
        self._hint(content, "Changing precision requires relaunching FlowClone.", 178, 512)

        self._section(content, "Hands-free", 476)
        self.handsfree = self._checkbox(
            content, "Quick-tap Right ⌘ to lock recording", 22, 444
        )
        self._label(content, "Stop after silence", 22, 410, 150)
        self.silence = NSSlider.alloc().initWithFrame_(NSMakeRect(178, 405, 265, 24))
        self.silence.setMinValue_(0.5)
        self.silence.setMaxValue_(10.0)
        self.silence.setContinuous_(True)
        self.silence.setTarget_(self)
        self.silence.setAction_("silenceChanged:")
        content.addSubview_(self.silence)
        self.silence_value = self._label(content, "", 454, 407, 74)

        self._section(content, "Cleanup", 370)
        self.cleanup_enabled = self._checkbox(
            content, "Remove fillers and clean transcript", 22, 338
        )
        self.dedupe = self._checkbox(content, "Collapse repeated words", 292, 338)
        self.context_aware = self._checkbox(
            content, "Join text using cursor context", 22, 308
        )
        self.scratch = self._checkbox(
            content, 'Enable “scratch that”', 292, 308
        )
        self.trailing = self._checkbox(content, "Always append a trailing space", 22, 278)

        self._label(content, "Extra fillers", 22, 244, 150)
        self.extra_fillers = NSTextField.alloc().initWithFrame_(
            NSMakeRect(178, 238, 350, 26)
        )
        self.extra_fillers.setPlaceholderString_("like, you know, basically")
        content.addSubview_(self.extra_fillers)

        self._section(content, "Personal dictionary", 204)
        self._hint(content, "One correction per line: spoken phrase = replacement", 22, 184)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(22, 66, 506, 112))
        scroll.setBorderType_(NSBezelBorder)
        scroll.setHasVerticalScroller_(True)
        self.dictionary = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 490, 112))
        self.dictionary.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0.0))
        scroll.setDocumentView_(self.dictionary)
        content.addSubview_(scroll)

        self.error = self._label(content, "", 22, 40, 350)
        self.error.setTextColor_(NSColor.systemRedColor())

        cancel = NSButton.alloc().initWithFrame_(NSMakeRect(378, 20, 72, 30))
        cancel.setTitle_("Cancel")
        cancel.setBezelStyle_(1)
        cancel.setTarget_(self)
        cancel.setAction_("cancel:")
        content.addSubview_(cancel)

        save = NSButton.alloc().initWithFrame_(NSMakeRect(456, 20, 72, 30))
        save.setTitle_("Save")
        save.setBezelStyle_(1)
        save.setKeyEquivalent_("\r")
        save.setTarget_(self)
        save.setAction_("save:")
        content.addSubview_(save)

    @objc.python_method
    def _label(self, content, text: str, x: float, y: float, width: float):
        label = NSTextField.labelWithString_(text)
        label.setFrame_(NSMakeRect(x, y, width, 20))
        content.addSubview_(label)
        return label

    @objc.python_method
    def _hint(self, content, text: str, x: float, y: float):
        label = self._label(content, text, x, y, 410)
        label.setFont_(NSFont.systemFontOfSize_(11))
        label.setTextColor_(NSColor.secondaryLabelColor())
        return label

    @objc.python_method
    def _section(self, content, text: str, y: float) -> None:
        label = self._label(content, text, 22, y, 300)
        label.setFont_(NSFont.boldSystemFontOfSize_(13))

    @objc.python_method
    def _checkbox(self, content, text: str, x: float, y: float):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, 255, 24))
        button.setButtonType_(NSSwitchButton)
        button.setTitle_(text)
        content.addSubview_(button)
        return button

    @objc.python_method
    def show(self, on_save) -> None:
        self._on_save = on_save
        settings = config.load_preferences()
        self._loaded_quantization = settings.quantization
        self.quantization.selectItemWithTitle_(settings.quantization)
        self._set_checked(self.handsfree, settings.handsfree_enabled)
        self.silence.setDoubleValue_(settings.silence_stop_seconds)
        self._update_silence_label()
        self._set_checked(self.cleanup_enabled, settings.cleanup_enabled)
        self._set_checked(self.dedupe, settings.dedupe_stutters)
        self._set_checked(self.context_aware, settings.context_aware)
        self._set_checked(self.scratch, settings.scratch_that)
        self._set_checked(self.trailing, settings.add_trailing_space)
        self.extra_fillers.setStringValue_(", ".join(settings.extra_fillers))
        self.dictionary.setString_(
            "\n".join(f"{spoken} = {replacement}" for spoken, replacement in settings.dictionary)
        )
        self.error.setStringValue_("")
        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    @objc.python_method
    def _set_checked(self, button, enabled: bool) -> None:
        button.setState_(NSControlStateValueOn if enabled else 0)

    @objc.python_method
    def _checked(self, button) -> bool:
        return button.state() == NSControlStateValueOn

    def silenceChanged_(self, _sender) -> None:
        self._update_silence_label()

    @objc.python_method
    def _update_silence_label(self) -> None:
        self.silence_value.setStringValue_(f"{self.silence.doubleValue():.1f} sec")

    @objc.python_method
    def _parse_dictionary(self) -> tuple[tuple[str, str], ...]:
        pairs = []
        seen = set()
        for line_number, raw_line in enumerate(self.dictionary.string().splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            if "=" not in line:
                raise ValueError(f"Dictionary line {line_number} needs an = sign.")
            spoken, replacement = (part.strip() for part in line.split("=", 1))
            if not spoken or not replacement:
                raise ValueError(f"Dictionary line {line_number} is incomplete.")
            normalized = spoken.casefold()
            if normalized in seen:
                raise ValueError(f"Dictionary line {line_number} duplicates {spoken!r}.")
            seen.add(normalized)
            pairs.append((spoken, replacement))
        return tuple(pairs)

    def save_(self, _sender) -> None:
        try:
            quantization = self.quantization.titleOfSelectedItem()
            fillers = tuple(
                value.strip()
                for value in self.extra_fillers.stringValue().replace("\n", ",").split(",")
                if value.strip()
            )
            settings = config.PreferencesConfig(
                quantization=quantization,
                handsfree_enabled=self._checked(self.handsfree),
                silence_stop_seconds=self.silence.doubleValue(),
                cleanup_enabled=self._checked(self.cleanup_enabled),
                dedupe_stutters=self._checked(self.dedupe),
                context_aware=self._checked(self.context_aware),
                scratch_that=self._checked(self.scratch),
                add_trailing_space=self._checked(self.trailing),
                extra_fillers=fillers,
                dictionary=self._parse_dictionary(),
            )
            config.save_preferences(settings)
        except (OSError, TypeError, ValueError) as exc:
            self.error.setStringValue_(str(exc))
            return
        restart_required = quantization != self._loaded_quantization
        self.window.orderOut_(None)
        self._on_save(restart_required)

    def cancel_(self, _sender) -> None:
        self.window.orderOut_(None)
