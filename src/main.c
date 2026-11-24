/* main.c
 *
 * Copyright 2020 Christian Hergert
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#define G_LOG_DOMAIN "main"

#include "config.h"

#include <glib/gi18n.h>

#include <gtk/gtk.h>
#include <gtksourceview/gtksource.h>

#ifdef HAVE_ENCHANT
# include <enchant.h>
#endif

#ifdef __APPLE__
# include <mach-o/dyld.h>
#endif

#include "build-ident.h"
#include "editor-application-private.h"

#ifdef __APPLE__
/* Returns Contents/Resources when running from inside a relocatable
 * macOS .app bundle (see build-aux/macos/bundle.py), otherwise NULL.
 */
static char *
get_macos_bundle_resources (void)
{
  g_autofree char *exe = NULL;
  g_autofree char *real = NULL;
  g_autofree char *macos_dir = NULL;
  g_autofree char *contents = NULL;
  g_autofree char *dir_name = NULL;
  g_autofree char *resources = NULL;
  uint32_t size = 0;

  _NSGetExecutablePath (NULL, &size);
  exe = g_malloc0 (size + 1);
  if (_NSGetExecutablePath (exe, &size) != 0)
    return NULL;

  if (!(real = realpath (exe, NULL)))
    return NULL;

  macos_dir = g_path_get_dirname (real);
  dir_name = g_path_get_basename (macos_dir);
  if (g_strcmp0 (dir_name, "MacOS") != 0)
    return NULL;

  contents = g_path_get_dirname (macos_dir);
  resources = g_build_filename (contents, "Resources", NULL);
  if (!g_file_test (resources, G_FILE_TEST_IS_DIR))
    return NULL;

  return g_steal_pointer (&resources);
}

static void
rebind_library_textdomains (GApplication *app,
                            const char   *localedir)
{
  /* GTK and friends bind their domains to their compile-time prefix
   * during startup, so this must run after the default handler.
   */
  static const char * const domains[] = {
    "gtk40", "libadwaita", "gtksourceview-5", "libspelling",
  };

  for (guint i = 0; i < G_N_ELEMENTS (domains); i++)
    bindtextdomain (domains[i], localedir);
}
#endif

static void
check_early_opts (int        *argc,
                  char     ***argv,
                  gboolean   *standalone,
                  gboolean   *exit_after_startup)
{
  g_autoptr(GOptionContext) context = NULL;
  gboolean version = FALSE;
  GOptionEntry entries[] = {
    { "standalone", 's', 0, G_OPTION_ARG_NONE, standalone },
    { "version", 0, 0, G_OPTION_ARG_NONE, &version },
    { "exit-after-startup", 0, G_OPTION_FLAG_HIDDEN, G_OPTION_ARG_NONE, exit_after_startup },
    { NULL }
  };

  context = g_option_context_new (NULL);
  g_option_context_set_ignore_unknown_options (context, TRUE);
  g_option_context_set_help_enabled (context, FALSE);
  g_option_context_add_main_entries (context, entries, NULL);
  g_option_context_parse (context, argc, argv, NULL);

  if (version)
    {
      g_print ("%s %s (%s)\n", PACKAGE_NAME, PACKAGE_VERSION, EDITOR_BUILD_IDENTIFIER);
      g_print ("\n");
      g_print ("            GTK: %d.%d.%d (Compiled against %d.%d.%d)\n",
                  gtk_get_major_version (), gtk_get_minor_version (), gtk_get_micro_version (),
                  GTK_MAJOR_VERSION, GTK_MINOR_VERSION, GTK_MICRO_VERSION);
      g_print ("  GtkSourceView: %d.%d.%d (Compiled against %d.%d.%d)\n",
                  gtk_source_get_major_version (), gtk_source_get_minor_version (), gtk_source_get_micro_version (),
                  GTK_SOURCE_MAJOR_VERSION, GTK_SOURCE_MINOR_VERSION, GTK_SOURCE_MICRO_VERSION);

#ifdef HAVE_ENCHANT
      g_print ("        Enchant: %s\n", enchant_get_version ());
#endif

      g_print ("\n\
Copyright 2020-2025 Christian Hergert, et al.\n\
This is free software; see the source for copying conditions. There is NO\n\
warranty; not even for MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.\n\
");

      exit (EXIT_SUCCESS);
    }
}

int
main (int   argc,
      char *argv[])
{
  g_autoptr(EditorApplication) app = NULL;
  gboolean standalone = FALSE;
  gboolean exit_after_startup = FALSE;
  const char *localedir = LOCALEDIR;
  int ret;
#ifdef __APPLE__
  g_autofree char *bundle_resources = get_macos_bundle_resources ();
  g_autofree char *bundle_localedir = NULL;

  if (bundle_resources != NULL)
    {
      bundle_localedir = g_build_filename (bundle_resources, "share", "locale", NULL);
      localedir = bundle_localedir;
# ifdef HAVE_ENCHANT
      /* Spell-checking providers live in Resources/lib/enchant-2 */
      enchant_set_prefix_dir (bundle_resources);
# endif
    }
#endif

  bindtextdomain (GETTEXT_PACKAGE, localedir);
  bind_textdomain_codeset (GETTEXT_PACKAGE, "UTF-8");
  textdomain (GETTEXT_PACKAGE);

  check_early_opts (&argc, &argv, &standalone, &exit_after_startup);

  app = _editor_application_new (standalone);

#ifdef __APPLE__
  if (bundle_localedir != NULL)
    g_signal_connect (app,
                      "startup",
                      G_CALLBACK (rebind_library_textdomains),
                      (gpointer) bundle_localedir);
#endif

  if (exit_after_startup)
    g_signal_connect_after (app,
                            "startup",
                            G_CALLBACK (g_application_activate),
                            NULL);

  ret = g_application_run (G_APPLICATION (app), argc, argv);

  gtk_source_finalize ();

  return ret;
}
