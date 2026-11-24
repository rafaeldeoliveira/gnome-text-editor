#!/usr/bin/env python3
"""Rewrite the keyboard shortcuts in a GtkBuilder .ui file for macOS.

Usage: macos-shortcuts.py INPUT OUTPUT

Run by src/meson.build on macOS for the .ui files that contain
accelerators: menu "accel" attributes, GtkShortcut "trigger" properties,
and the entries of the shortcuts dialog. The C key bindings use
EDITOR_PRIMARY_MASK (src/editor-utils-private.h) and a few macOS-only
bindings; the rules here must describe the same shortcuts.

The main rule is Control -> Command. The exceptions are shortcuts that
macOS reserves or spells differently, and shortcuts provided by GTK,
libadwaita or GtkSourceView that keep using Control on macOS.
"""

import re
import sys

MODIFIER_NAMES = {
    'control': 'Control', 'ctrl': 'Control', 'primary': 'Control',
    'shift': 'Shift', 'alt': 'Alt', 'mod1': 'Alt', 'meta': 'Meta',
    'super': 'Super',
}
MODIFIER_ORDER = ['Control', 'Alt', 'Shift', 'Meta', 'Super']

# Full replacements, keyed by (sorted modifiers, key).
MAC_EQUIVALENTS = {
    # Command-H hides the application; Mac editors use Command-Option-F.
    (('Control',), 'h'): (('Meta', 'Alt'), 'f'),
    # Command-Option-D shows/hides the Dock.
    (('Alt', 'Control'), 'd'): (('Meta', 'Shift'), 'd'),
    # Option-W types a character; TextMate uses Command-Option-W for wrapping.
    (('Alt',), 'w'): (('Meta', 'Alt'), 'w'),
    # GtkTextView's macOS text navigation and deletion keys.
    (('Control',), 'Home'): (('Meta',), 'Up'),
    (('Control',), 'End'): (('Meta',), 'Down'),
    (('Control',), 'BackSpace'): (('Alt',), 'BackSpace'),
    (('Control',), 'Delete'): (('Alt',), 'Delete'),
    (('Control', 'Shift'), 'BackSpace'): (('Meta',), 'BackSpace'),
    (('Control', 'Shift'), 'Delete'): (('Meta',), 'Delete'),
}

# Shortcuts that stay on Control: tab switching (libadwaita and
# EditorSourceView, as in Safari and Terminal), paragraph movement and the
# emoji chooser (GtkTextView binds these with Control on every platform).
KEEP_CONTROL_KEYS = {'Page_Up', 'Page_Down', 'Tab', 'Up', 'Down', 'period', 'semicolon'}

# Shortcuts dialog entries bound by GtkSourceView, which uses Control on
# every platform. Matched by title because "Unselect all" shares the same
# accelerator but comes from GtkTextView, which uses Command on macOS.
KEEP_CONTROL_TITLES = {
    'Increment number at cursor',
    'Decrement number at cursor',
}


def parse(accel):
    mods = []
    rest = accel
    while (m := re.match(r'<([A-Za-z0-9]+)>', rest)):
        name = MODIFIER_NAMES.get(m.group(1).lower())
        if name is None:
            raise SystemExit(f'error: unknown modifier in {accel!r}')
        mods.append(name)
        rest = rest[m.end():]
    return tuple(sorted(set(mods))), rest


def unparse(mods, key):
    ordered = sorted(mods, key=MODIFIER_ORDER.index)
    return ''.join(f'<{m}>' for m in ordered) + key


def translate(accel, keep_control=False):
    if not accel.startswith('<'):
        return accel  # F1, Insert, ...
    mods, key = parse(accel)
    if (mods, key) in MAC_EQUIVALENTS:
        return unparse(*MAC_EQUIVALENTS[(mods, key)])
    if keep_control or 'Control' not in mods or key in KEEP_CONTROL_KEYS:
        return accel
    return unparse(tuple('Meta' if m == 'Control' else m for m in mods), key)


def escape(accel):
    return accel.replace('<', '&lt;').replace('>', '&gt;')


def unescape(accel):
    return accel.replace('&lt;', '<').replace('&gt;', '>')


def rewrite_value(match, keep_control=False):
    old = unescape(match.group(2))
    return match.group(1) + escape(translate(old, keep_control)) + match.group(3)


ACCEL_RE = re.compile(
    r'(<(?:attribute name="accel"|property name="trigger"|property name="accelerator")[^>]*>)'
    r'([^<]*)(</(?:attribute|property)>)')


def rewrite_shortcuts_item(match):
    block = match.group(0)
    title = re.search(r'name="title"[^>]*>([^<]*)<', block)
    keep = title is not None and title.group(1) in KEEP_CONTROL_TITLES
    return ACCEL_RE.sub(lambda m: rewrite_value(m, keep), block)


def main():
    src, dest = sys.argv[1:3]
    with open(src, encoding='utf-8') as f:
        ui = f.read()

    # Shortcuts dialog entries first, so their titles can be consulted;
    # then every other accelerator in the file.
    item_re = re.compile(r'<object class="AdwShortcutsItem">.*?</object>', re.S)
    ui = item_re.sub(rewrite_shortcuts_item, ui)
    parts = item_re.split(ui)
    items = item_re.findall(ui)
    parts = [ACCEL_RE.sub(rewrite_value, p) for p in parts]
    ui = ''.join(p + (items[i] if i < len(items) else '') for i, p in enumerate(parts))

    with open(dest, 'w', encoding='utf-8') as f:
        f.write(ui)


if __name__ == '__main__':
    main()
