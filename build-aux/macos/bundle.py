#!/usr/bin/env python3
"""Build a self-contained "Text Editor.app" for macOS.

Usage (from the source tree, with the Homebrew dependencies installed):

    build-aux/macos/bundle.py              # release build -> _build-macos/
    build-aux/macos/bundle.py --devel      # org.gnome.TextEditor.Devel
    build-aux/macos/bundle.py --install    # also copy to /Applications

The app is compiled with meson, then every non-system library it loads
(directly, or through the enchant and gdk-pixbuf plugins it needs) is
copied into Contents/Frameworks and rewritten to load through @rpath.
The data those libraries look for at runtime (settings schemas, icons,
syntax definitions, translations, fontconfig setup) is copied into
Contents/Resources, and Contents/MacOS/gnome-text-editor is a small
launcher that points each library at it. The result does not need
Homebrew to run and can be moved anywhere.

Required Homebrew packages:
    brew install meson ninja pkg-config gtk4 libadwaita gtksourceview5 \\
                 libspelling editorconfig itstool desktop-file-utils \\
                 adwaita-icon-theme librsvg
"""

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

SRCDIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SYSTEM_PREFIXES = ('/usr/lib/', '/System/')

# Translation domains loaded at runtime (see rebind_library_textdomains()
# in src/main.c).
GETTEXT_DOMAINS = ('gnome-text-editor', 'gtk40', 'libadwaita',
                   'gtksourceview-5', 'libspelling')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def quiet(*args):
    """Run a command, only showing its output if it fails."""
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f'error: {args[0]} failed on {args[-1]}')


def output(*args):
    return run(*args, capture_output=True).stdout


def log(msg):
    print(f'==> {msg}', flush=True)


# --------------------------------------------------------------------------
# Mach-O handling
# --------------------------------------------------------------------------

def load_commands(path):
    """Return (dependencies, rpaths) recorded in a Mach-O file."""
    deps, rpaths = [], []
    cmd = None
    for line in output('otool', '-l', path).splitlines():
        line = line.strip()
        if line.startswith('cmd '):
            cmd = line.split()[1]
        elif line.startswith('name ') and cmd in ('LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB',
                                                  'LC_REEXPORT_DYLIB', 'LC_LOAD_UPWARD_DYLIB'):
            deps.append(line[5:line.rindex(' (offset')])
        elif line.startswith('path ') and cmd == 'LC_RPATH':
            rpaths.append(line[5:line.rindex(' (offset')])
    return deps, rpaths


def resolve(dep, image, rpaths):
    """Find the file a load command refers to, or None for system libraries."""
    if dep.startswith(SYSTEM_PREFIXES):
        return None
    loader_dir = os.path.dirname(image)
    if dep.startswith('@loader_path/'):
        candidates = [os.path.join(loader_dir, dep[len('@loader_path/'):])]
    elif dep.startswith('@rpath/'):
        rest = dep[len('@rpath/'):]
        candidates = [os.path.join(rp.replace('@loader_path', loader_dir), rest)
                      for rp in rpaths]
    elif dep.startswith('@'):
        raise SystemExit(f'error: cannot resolve {dep} from {image}')
    else:
        candidates = [dep]
    for c in candidates:
        if os.path.exists(c):
            return os.path.realpath(c)
    raise SystemExit(f'error: {dep} (needed by {image}) not found')


class Bundler:
    def __init__(self, contents):
        self.contents = contents
        self.frameworks = os.path.join(contents, 'Frameworks')
        os.makedirs(self.frameworks, exist_ok=True)
        self.bundled = {}   # bundle file name -> source real path
        self.images = []    # (dest path, rpath to Frameworks, is_dylib_in_frameworks)

    def _bundle_name(self, dep, real):
        # Keep the install name's basename (e.g. libgio-2.0.0.dylib), which
        # is what other libraries refer to.
        name = os.path.basename(dep)
        if self.bundled.get(name, real) != real:
            raise SystemExit(f'error: two different libraries named {name}')
        return name

    def add_image(self, src, dest, rpath):
        """Copy a Mach-O file into the bundle and queue its dependencies."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        os.chmod(dest, 0o755)
        self.images.append((dest, src, rpath))

    def process(self):
        """Pull in every dependency, then rewrite all load commands."""
        i = 0
        while i < len(self.images):
            dest, src, rpath = self.images[i]
            i += 1
            deps, rpaths = load_commands(src)
            for dep in deps:
                real = resolve(dep, src, rpaths)
                if real is None:
                    continue
                name = self._bundle_name(dep, real)
                if name not in self.bundled:
                    self.bundled[name] = real
                    self.add_image(real, os.path.join(self.frameworks, name), '@loader_path')

        log(f'Rewriting {len(self.images)} binaries ({len(self.bundled)} libraries bundled)')
        for dest, src, rpath in self.images:
            self._rewrite(dest, rpath)

    def _rewrite(self, path, rpath):
        deps, rpaths = load_commands(path)
        args = []
        if os.path.dirname(path) == self.frameworks:
            args += ['-id', '@rpath/' + os.path.basename(path)]
        for dep in deps:
            if not dep.startswith(SYSTEM_PREFIXES):
                args += ['-change', dep, '@rpath/' + os.path.basename(dep)]
        for rp in dict.fromkeys(rpaths):
            args += ['-delete_rpath', rp]
        if args:
            quiet('install_name_tool', *args, path)
        quiet('install_name_tool', '-add_rpath', rpath, path)
        quiet('codesign', '--force', '--sign', '-', path)


# --------------------------------------------------------------------------
# Data files
# --------------------------------------------------------------------------

def copytree(src, dest, ignore=None):
    shutil.copytree(src, dest, symlinks=False, ignore=ignore, dirs_exist_ok=True)


def copy_schemas(stage, brew, share):
    schemas = os.path.join(share, 'glib-2.0', 'schemas')
    os.makedirs(schemas, exist_ok=True)
    for d in (os.path.join(stage, 'share/glib-2.0/schemas'),
              os.path.join(brew, 'share/glib-2.0/schemas')):
        for f in os.listdir(d):
            if f.endswith('.xml') and (f.startswith('org.gnome.TextEditor') or
                                       f.startswith('org.gtk.gtk4.')):
                shutil.copy2(os.path.join(d, f), schemas)
    run('glib-compile-schemas', schemas)


def copy_icons(stage, brew, share):
    icons = os.path.join(share, 'icons')
    copytree(os.path.join(brew, 'share/icons/Adwaita'), os.path.join(icons, 'Adwaita'),
             ignore=shutil.ignore_patterns('cursors', 'icon-theme.cache'))
    hicolor = os.path.join(icons, 'hicolor')
    os.makedirs(hicolor, exist_ok=True)
    shutil.copy2(os.path.join(brew, 'share/icons/hicolor/index.theme'), hicolor)
    copytree(os.path.join(stage, 'share/icons/hicolor'), hicolor,
             ignore=shutil.ignore_patterns('icon-theme.cache'))
    for theme in ('Adwaita', 'hicolor'):
        run('gtk4-update-icon-cache', '-q', '-t', '-f', os.path.join(icons, theme))


def copy_locales(stage, brew, share):
    count = 0
    for root in (os.path.join(brew, 'share/locale'), os.path.join(stage, 'share/locale')):
        if not os.path.isdir(root):
            continue
        for lang in os.listdir(root):
            for domain in GETTEXT_DOMAINS:
                mo = os.path.join(root, lang, 'LC_MESSAGES', domain + '.mo')
                if os.path.exists(mo):
                    dest = os.path.join(share, 'locale', lang, 'LC_MESSAGES')
                    os.makedirs(dest, exist_ok=True)
                    shutil.copy2(os.path.realpath(mo), dest)
                    count += 1
    log(f'Copied {count} translation catalogs')


def copy_fontconfig(brew, resources):
    src = os.path.join(brew, 'etc/fonts')
    dest = os.path.join(resources, 'etc/fonts')
    copytree(os.path.join(src, 'conf.d'), os.path.join(dest, 'conf.d'),
             ignore=shutil.ignore_patterns('README'))
    with open(os.path.join(src, 'fonts.conf')) as f:
        conf = f.read()
    # Drop Homebrew's own font and cache directories; keep the system ones.
    conf = re.sub(r'\s*<(dir|cachedir)>' + re.escape(brew) + r'[^<]*</\1>', '', conf)
    with open(os.path.join(dest, 'fonts.conf'), 'w') as f:
        f.write(conf)


# --------------------------------------------------------------------------
# Bundle skeleton
# --------------------------------------------------------------------------

LAUNCHER = r'''#!/bin/sh
# Points GTK and its libraries at the data inside this bundle, then runs
# the real binary. Generated by build-aux/macos/bundle.py.

macos="$(cd "$(dirname "$0")" && pwd -P)"
res="$(cd "$macos/../Resources" && pwd -P)"

export XDG_DATA_DIRS="$res/share"
export GSETTINGS_SCHEMA_DIR="$res/share/glib-2.0/schemas"
export GTK_DATA_PREFIX="$res"
export GTK_EXE_PREFIX="$res"
export GTK_PATH="$res/lib/gtk-4.0"
export GIO_MODULE_DIR="$res/lib/gio/modules"
export FONTCONFIG_PATH="$res/etc/fonts"
export FONTCONFIG_FILE="$res/etc/fonts/fonts.conf"

# gdk-pixbuf needs absolute module paths, so fill in where we live now.
cache_dir="$HOME/Library/Caches/@APP_ID@"
mkdir -p "$cache_dir"
sed "s|@RESOURCES@|$res|g" "$res/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache.in" \
  > "$cache_dir/loaders.cache"
export GDK_PIXBUF_MODULEDIR="$res/lib/gdk-pixbuf-2.0/2.10.0/loaders"
export GDK_PIXBUF_MODULE_FILE="$cache_dir/loaders.cache"

# Apps started from Finder get no LANG; derive one from System Settings.
if [ -z "${LANG:-}" ]; then
  locale_id="$(defaults read -g AppleLocale 2>/dev/null | sed 's/@.*//')"
  if [ -n "$locale_id" ] && locale -a | grep -qx "$locale_id.UTF-8"; then
    export LANG="$locale_id.UTF-8"
  else
    export LANG="en_US.UTF-8"
  fi
fi
if [ -z "${LANGUAGE:-}" ]; then
  languages="$(defaults read -g AppleLanguages 2>/dev/null \
    | tr -d ' "(),' | grep -v '^$' | tr '-' '_' | paste -sd: -)"
  [ -n "$languages" ] && export LANGUAGE="$languages"
fi

# Finder used to pass a process serial number argument; drop it.
case "${1:-}" in -psn_*) shift ;; esac

exec "$macos/gnome-text-editor-bin" "$@"
'''


def write_info_plist(contents, app_id, name, version):
    info = {
        'CFBundleName': name,
        'CFBundleDisplayName': name,
        'CFBundleIdentifier': app_id,
        'CFBundleExecutable': 'gnome-text-editor',
        # Recent macOS no longer appends the extension itself, and falls
        # back to a blank placeholder icon without it.
        'CFBundleIconFile': app_id + '.icns',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        'CFBundleDevelopmentRegion': 'en',
        'LSApplicationCategoryType': 'public.app-category.developer-tools',
        'LSMinimumSystemVersion': output('sw_vers', '-productVersion').strip().split('.')[0] + '.0',
        'NSHighResolutionCapable': True,
        'NSHumanReadableCopyright': 'Copyright 2020-2025 Christian Hergert, et al. GPL-3.0-or-later',
        'CFBundleDocumentTypes': [{
            'CFBundleTypeName': 'Text Document',
            'CFBundleTypeRole': 'Editor',
            'LSHandlerRank': 'Alternate',
            'LSItemContentTypes': ['public.text', 'public.plain-text',
                                   'public.source-code', 'public.script',
                                   'public.json', 'public.xml'],
        }],
    }
    with open(os.path.join(contents, 'Info.plist'), 'wb') as f:
        plistlib.dump(info, f)


def write_icns(resources, app_id):
    svg = os.path.join(SRCDIR, 'data/icons', app_id + '.svg')
    with tempfile.TemporaryDirectory() as tmp:
        iconset = os.path.join(tmp, 'icon.iconset')
        os.mkdir(iconset)
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ''), (2, '@2x')):
                px = size * scale
                run('rsvg-convert', '-w', str(px), '-h', str(px), svg,
                    '-o', os.path.join(iconset, f'icon_{size}x{size}{suffix}.png'))
        run('iconutil', '-c', 'icns', iconset,
            '-o', os.path.join(resources, app_id + '.icns'))


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--builddir', default=os.path.join(SRCDIR, '_build-macos'),
                        help='meson build directory (default: _build-macos)')
    parser.add_argument('--devel', action='store_true',
                        help='make a development build (separate app id and settings)')
    parser.add_argument('--install', action='store_true',
                        help='copy the finished app into /Applications')
    args = parser.parse_args()

    brew = output('brew', '--prefix').strip()
    builddir = os.path.abspath(args.builddir)
    stage = os.path.join(builddir, 'stage')

    # Build ------------------------------------------------------------------
    log('Building with meson')
    setup = ['meson', 'setup', builddir, SRCDIR, f'--prefix={stage}',
             '--buildtype=release', f'-Ddevelopment={str(args.devel).lower()}',
             '--wrap-mode=nodownload']
    if os.path.exists(os.path.join(builddir, 'build.ninja')):
        setup.append('--reconfigure')
    run(*setup, stdout=subprocess.DEVNULL)
    shutil.rmtree(stage, ignore_errors=True)
    run('meson', 'install', '-C', builddir, '--quiet')

    app_id = 'org.gnome.TextEditor.Devel' if args.devel else 'org.gnome.TextEditor'
    name = 'Text Editor (Devel)' if args.devel else 'Text Editor'
    version = re.search(r"version:\s*'([^']+)'",
                        open(os.path.join(SRCDIR, 'meson.build')).read()).group(1)

    app = os.path.join(builddir, name + '.app')
    shutil.rmtree(app, ignore_errors=True)
    contents = os.path.join(app, 'Contents')
    resources = os.path.join(contents, 'Resources')
    share = os.path.join(resources, 'share')
    log(f'Assembling {app}')

    # Binaries ---------------------------------------------------------------
    bundler = Bundler(contents)
    bundler.add_image(os.path.join(stage, 'bin/gnome-text-editor'),
                      os.path.join(contents, 'MacOS/gnome-text-editor-bin'),
                      '@executable_path/../Frameworks')

    def add_module(src, dest_dir):
        dest = os.path.join(resources, dest_dir, os.path.basename(src))
        to_fw = os.path.relpath(bundler.frameworks, os.path.dirname(dest))
        bundler.add_image(src, dest, '@loader_path/' + to_fw)

    # Spell checking via the system dictionaries (NSSpellChecker).
    enchant_prefix = os.path.realpath(os.path.join(brew, 'opt/enchant'))
    add_module(os.path.join(enchant_prefix, 'lib/enchant-2/enchant_applespell.so'),
               'lib/enchant-2')
    copytree(os.path.join(enchant_prefix, 'share/enchant-2'), os.path.join(share, 'enchant-2'))

    # SVG support for gdk-pixbuf (non-symbolic icons such as the app icon).
    loaders = 'lib/gdk-pixbuf-2.0/2.10.0/loaders'
    svg_loader = os.path.join(brew, loaders, 'libpixbufloader_svg.so')
    add_module(svg_loader, loaders)
    cache = output(os.path.join(brew, 'opt/gdk-pixbuf/bin/gdk-pixbuf-query-loaders'), svg_loader)
    cache = re.sub(r'"[^"]*/(libpixbufloader[^"/]*)"', rf'"@RESOURCES@/{loaders}/\1"', cache)
    cache = '\n'.join(l for l in cache.splitlines() if not l.startswith('#')) + '\n'
    with open(os.path.join(resources, 'lib/gdk-pixbuf-2.0/2.10.0/loaders.cache.in'), 'w') as f:
        f.write(cache)

    bundler.process()

    launcher = os.path.join(contents, 'MacOS/gnome-text-editor')
    with open(launcher, 'w') as f:
        f.write(LAUNCHER.replace('@APP_ID@', app_id))
    os.chmod(launcher, 0o755)

    # Data -------------------------------------------------------------------
    log('Copying data files')
    copy_schemas(stage, brew, share)
    copy_icons(stage, brew, share)
    copy_locales(stage, brew, share)
    copytree(os.path.join(os.path.realpath(os.path.join(brew, 'opt/gtksourceview5')),
                          'share/gtksourceview-5'),
             os.path.join(share, 'gtksourceview-5'))
    copy_fontconfig(brew, resources)
    os.makedirs(os.path.join(resources, 'lib/gio/modules'), exist_ok=True)
    os.makedirs(os.path.join(resources, 'lib/gtk-4.0'), exist_ok=True)

    write_info_plist(contents, app_id, name, version)
    write_icns(resources, app_id)

    log('Signing (ad-hoc)')
    quiet('codesign', '--force', '--deep', '--sign', '-', app)

    # Sanity check: nothing may still point into Homebrew.
    leaks = []
    for dest, _, _ in bundler.images:
        deps, rpaths = load_commands(dest)
        leaks += [f'{dest}: {p}' for p in deps + rpaths if p.startswith(brew)]
    if leaks:
        raise SystemExit('error: bundle still references Homebrew:\n  ' + '\n  '.join(leaks))

    size = output('du', '-sh', app).split()[0]
    log(f'Done: {app} ({size})')

    if args.install:
        target = os.path.join('/Applications', name + '.app')
        log(f'Installing to {target}')
        shutil.rmtree(target, ignore_errors=True)
        run('ditto', app, target)
        # Make LaunchServices pick up the new icon and document types.
        run('/System/Library/Frameworks/CoreServices.framework/Frameworks/'
            'LaunchServices.framework/Support/lsregister', '-f', target)


if __name__ == '__main__':
    main()
