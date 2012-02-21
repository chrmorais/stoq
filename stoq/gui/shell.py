# -*- coding: utf-8 *-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2005-2012 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation; either version 2 of the License, or
## (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Stoq Team <stoq-devel@async.com.br>
##
##

""" Stoq shell routines"""

import gettext
import locale
import logging
import operator
import os
import platform
import sys

_ = gettext.gettext
_shell = None
log = logging.getLogger('stoq.shell')
PRIVACY_STRING = _(
    "One of the new features of Stoq 1.0 is support for online "
    "services. Features using the online services include automatic "
    "bug report and update notifications. More services are under development."
    "To be able to provide a better service and properly identify the user "
    "we will collect the CNPJ of the primary branch and the ip address.\n\n"
    "<b>We will not disclose the collected information and we are committed "
    "to keeping your privacy intact.</b>")


class Shell(object):
    def __init__(self, options):
        global _shell
        _shell = self
        self._application_cache = {}
        self._appname = None
        self._blocked_apps = []
        self._current_app = None
        self._cur_exit_func = None
        self._hidden_apps = []
        self._login = None
        self._log_filename = None
        self._options = options
        self._ran_wizard = False
        self._restart = False
        self._stream = None
        self._user = None

    def _bootstrap(self):
        self._set_uptime()
        # Do this as soon as possible, before we attempt to use the
        # external libraries/resources
        self._set_user_locale()
        # Do this as early as possible to get as much as possible into the
        # log file itself, which means we cannot depend on the config or
        # anything else
        self._prepare_logfiles()
        self._set_app_info()
        self._check_dependencies()
        self._show_splash()
        self._setup_gtk()
        self._setup_twisted()
        self._check_version_policy()
        self._setup_ui_dialogs()
        self._setup_cookiefile()
        self._register_stock_icons()
        self._setup_exception_hook()
        self._setup_exit_hook()
        self._setup_database()
        self._load_key_bindings()
        self._setup_debug_options()

    #
    # Private
    #

    def _set_uptime(self):
        from stoqlib.lib.uptime import set_initial
        set_initial()

    def _set_user_locale(self):
        from stoqlib.lib.settings import get_settings

        settings = get_settings()
        lang = settings.get('user-locale', None)
        if not lang:
            return

        lang += '.UTF-8'
        try:
            locale.setlocale(locale.LC_ALL, lang)
        except locale.Error as err:
            log.warning("Could not set locale to %s. Error message: %s" %
                        (lang, err))
        else:
            os.environ['LANG'] = lang
            os.environ['LANGUAGE'] = lang

    def _prepare_logfiles(self):
        from stoq.lib.logging import setup_logging
        self._log_filename, self._stream = setup_logging("stoq")

    def _set_app_info(self):
        from kiwi.component import provide_utility
        from stoqlib.lib.interfaces import IAppInfo
        from stoqlib.lib.appinfo import AppInfo
        import stoq
        stoq_version = stoq.version
        if hasattr(stoq.library, 'get_revision'):
            rev = stoq.library.get_revision()
            if rev is not None:
                stoq_version += ' r' + rev
        info = AppInfo()
        info.set("name", "Stoq")
        info.set("version", stoq_version)
        info.set("log", self._log_filename)
        provide_utility(IAppInfo, info)

    def _check_dependencies(self):
        from stoq.lib.dependencies import check_dependencies
        check_dependencies()

    def _show_splash(self):
        from stoqlib.gui.splash import show_splash
        show_splash()

    def _setup_gtk(self):
        import gtk
        from kiwi.environ import environ

        # Total madness to make sure we can draw treeview lines,
        # this affects the GtkTreeView::grid-line-pattern style property
        #
        # Two bytes are sent in, see gtk_tree_view_set_grid_lines in gtktreeview.c
        # Byte 1 should be as high as possible, gtk+ 0x7F appears to be
        #        the highest allowed for Gtk+ 2.22 while 0xFF worked in
        #        earlier versions
        # Byte 2 should ideally be allowed to be 0, but neither C nor Python
        #        allows that.
        #
        data = environ.get_resource_string("stoq", "misc", "stoq.gtkrc")
        data = data.replace('\\x7f\\x01', '\x7f\x01')

        gtk.rc_parse_string(data)

        # Creating a button as a temporary workaround for bug
        # https://bugzilla.gnome.org/show_bug.cgi?id=632538, until gtk 3.0
        gtk.Button()
        settings = gtk.settings_get_default()
        settings.props.gtk_button_images = True

    def _setup_twisted(self):
        from twisted.internet import gtk2reactor
        gtk2reactor.install()

    def _check_version_policy(self):
        # No need to bother version checking when not running in developer mode
        from stoqlib.lib.parameters import is_developer_mode
        if not is_developer_mode():
            return

        import stoq

        #
        # Policies for stoq/stoqlib versions,
        # All these policies here are made so that stoqlib version is tightly
        # tied to the stoq versioning
        #

        # We reserve the first 89 for the stable series.
        FIRST_UNSTABLE_EXTRA_VERSION = 90

        # Stable series of Stoq must:
        # 1) have extra_version set to < 90
        # 2) Depend on a stoqlib version with extra_version < 90
        #
        if stoq.stable:
            if stoq.extra_version >= FIRST_UNSTABLE_EXTRA_VERSION:
                raise SystemExit(
                    "Stable stoq release should set extra_version to %d or lower" % (
                    FIRST_UNSTABLE_EXTRA_VERSION, ))

        # Unstable series of Stoq must have:
        # 1) have extra_version set to >= 90
        # 2) Must depend stoqlib version with extra_version >= 90
        #
        else:
            if stoq.extra_version < FIRST_UNSTABLE_EXTRA_VERSION:
                raise SystemExit(
                   "Unstable stoq (%s) must set extra_version to %d or higher, "
                   "or did you forget to set stoq.stable to True?" % (
                   stoq.version, FIRST_UNSTABLE_EXTRA_VERSION))

    def _setup_ui_dialogs(self):
        # This needs to be here otherwise we can't install the dialog
        if 'STOQ_TEST_MODE' in os.environ:
            return
        log.debug('providing graphical notification dialogs')
        from stoqlib.gui.base.dialogs import DialogSystemNotifier
        from stoqlib.lib.interfaces import ISystemNotifier
        from kiwi.component import provide_utility
        provide_utility(ISystemNotifier, DialogSystemNotifier(), replace=True)

        import gtk
        from kiwi.environ import environ
        from kiwi.ui.pixbufutils import pixbuf_from_string
        data = environ.get_resource_string('stoq', 'pixmaps', 'stoq-stock-app-24x24.png')
        gtk.window_set_default_icon(pixbuf_from_string(data))

        if platform.system() == 'Darwin':
            from AppKit import NSApplication, NSData, NSImage
            bytes = environ.get_resource_string('stoq', 'pixmaps', 'stoq-stock-app-48x48.png')
            data = NSData.alloc().initWithBytes_length_(bytes, len(bytes))
            icon = NSImage.alloc().initWithData_(data)
            app = NSApplication.sharedApplication()
            app.setApplicationIconImage_(icon)

    def _setup_cookiefile(self):
        log.debug('setting up cookie file')
        from kiwi.component import provide_utility
        from stoqlib.lib.cookie import Base64CookieFile
        from stoqlib.lib.interfaces import ICookieFile
        from stoqlib.lib.osutils import get_application_dir
        app_dir = get_application_dir()
        cookiefile = os.path.join(app_dir, "cookie")
        provide_utility(ICookieFile, Base64CookieFile(cookiefile))

    def _setup_exception_hook(self):
        if self._options.debug:
            hook = self._debug_hook
        else:
            hook = self._write_exception_hook
        sys.excepthook = hook

    def _setup_exit_hook(self):
        # Save any exiting exitfunc already set.
        if hasattr(sys, 'exitfunc'):
            self._cur_exit_func = sys.exitfunc
        sys.exitfunc = self._exit_func

    def _setup_database(self):
        from stoqlib.lib.configparser import StoqConfig
        from stoqlib.lib.message import error
        log.debug('reading configuration')
        config = StoqConfig()
        if self._options.filename:
            config.load(self._options.filename)
        else:
            config.load_default()
        config_file = config.get_filename()

        if self._options.wizard or not os.path.exists(config_file):
            self._run_first_time_wizard()

        if config.get('Database', 'enable_production') == 'True':
            self._run_first_time_wizard(config)

        settings = config.get_settings()

        try:
            conn_uri = settings.get_connection_uri()
        except:
            type, value, trace = sys.exc_info()
            error(_("Could not open the database config file"),
                  _("Invalid config file settings, got error '%s', "
                    "of type '%s'") % (value, type))

        from stoqlib.exceptions import StoqlibError
        from stoqlib.database.exceptions import PostgreSQLError
        from stoqlib.database.migration import needs_schema_update
        from stoq.lib.startup import setup
        log.debug('calling setup()')

        # XXX: progress dialog for connecting (if it takes more than
        # 2 seconds) or creating the database
        try:
            setup(config, self._options, register_station=True,
                  check_schema=False)
            if needs_schema_update():
                self._run_update_wizard()
        except (StoqlibError, PostgreSQLError), e:
            error(_('Could not connect to the database'),
                  'error=%s uri=%s' % (str(e), conn_uri))
            raise SystemExit("Error: bad connection settings provided")

    def _load_key_bindings(self):
        from stoqlib.gui.keybindings import load_user_keybindings
        load_user_keybindings()

    def _setup_debug_options(self):
        if not self._options.debug:
            return
        from gtk import keysyms
        from stoqlib.gui.keyboardhandler import install_global_keyhandler
        from stoqlib.gui.introspection import introspect_slaves
        install_global_keyhandler(keysyms.F12, introspect_slaves)

    def _register_stock_icons(self):
        from stoqlib.gui.stockicons import register

        log.debug('register stock icons')
        register()

    def _run_first_time_wizard(self, config=None):
        from stoqlib.gui.base.dialogs import run_dialog
        from stoq.gui.config import FirstTimeConfigWizard
        from stoqlib.gui.splash import hide_splash
        self._ran_wizard = True
        hide_splash()
        # This may run Stoq
        run_dialog(FirstTimeConfigWizard, None, self._options, config)

    def _run_update_wizard(self):
        from stoqlib.gui.base.dialogs import run_dialog
        from stoq.gui.update import SchemaUpdateWizard
        from stoqlib.gui.splash import hide_splash
        hide_splash()
        retval = run_dialog(SchemaUpdateWizard, None)
        if not retval:
            raise SystemExit()

    def _do_login(self):
        from stoqlib.exceptions import LoginError
        from stoqlib.gui.login import LoginHelper
        from stoqlib.lib.message import error
        self._login = LoginHelper(username=self._options.login_username)
        try:
            if not self.login():
                return
        except LoginError, e:
            error(e)
        self._check_param_main_branch()
        self._check_param_online_services()
        self._maybe_show_welcome_dialog()

    def _check_param_main_branch(self):
        from stoqlib.database.runtime import (get_connection, new_transaction,
                                              get_current_station)
        from stoqlib.domain.person import Company
        from stoqlib.lib.parameters import sysparam
        from stoqlib.lib.message import info
        conn = get_connection()
        compaines = Company.select(connection=conn)
        if (compaines.count() == 0 or
            not sysparam(conn).MAIN_COMPANY):
            from stoqlib.gui.base.dialogs import run_dialog
            from stoqlib.gui.dialogs.branchdialog import BranchDialog
            if self._ran_wizard:
                info(_("You need to register a company before start using Stoq"))
            else:
                info(_("Could not find a company. You'll need to register one "
                       "before start using Stoq"))
            trans = new_transaction()
            person = run_dialog(BranchDialog, None, trans)
            if not person:
                raise SystemExit
            branch = person.branch
            sysparam(trans).MAIN_COMPANY = branch.id
            get_current_station(trans).branch = branch
            trans.commit()

    def _check_param_online_services(self):
        from stoqlib.database.runtime import new_transaction
        from stoqlib.lib.parameters import sysparam
        import gtk

        trans = new_transaction()
        sparam = sysparam(trans)
        val = sparam.ONLINE_SERVICES
        if val is None:
            from kiwi.ui.dialogs import HIGAlertDialog
            # FIXME: All of this is to avoid having to set markup as the default
            #        in kiwi/ui/dialogs:HIGAlertDialog.set_details, after 1.0
            #        this can be simplified when we fix so that all descriptions
            #        sent to these dialogs are properly escaped
            dialog = HIGAlertDialog(
                parent=None,
                flags=gtk.DIALOG_MODAL,
                type=gtk.MESSAGE_WARNING)
            dialog.add_button(_("Not right now"), gtk.RESPONSE_NO)
            dialog.add_button(_("Enable online services"), gtk.RESPONSE_YES)

            dialog.set_primary(_('Do you want to enable Stoq online services?'))
            dialog.set_details(PRIVACY_STRING, use_markup=True)
            dialog.set_default_response(gtk.RESPONSE_YES)
            response = dialog.run()
            dialog.destroy()
            sparam.ONLINE_SERVICES = int(bool(response == gtk.RESPONSE_YES))
        trans.commit()

    def _maybe_show_welcome_dialog(self):
        from stoqlib.api import api
        if not api.user_settings.get('show-welcome-dialog', True):
            return
        api.user_settings.set('show-welcome-dialog', False)

        from stoq.gui.welcomedialog import WelcomeDialog
        from stoqlib.gui.base.dialogs import run_dialog
        run_dialog(WelcomeDialog)

    def _load_app(self, appdesc, app_window):
        import gtk
        module = __import__("stoq.gui.%s" % (appdesc.name, ),
                            globals(), locals(), [''])
        window = appdesc.name.capitalize() + 'App'
        window_class = getattr(module, window, None)
        if window_class is None:
            raise SystemExit("%s app misses a %r attribute" % (
                appdesc.name, window))

        from stoqlib.gui.splash import hide_splash
        hide_splash()

        embedded = getattr(window_class, 'embedded', False)
        from stoq.gui.application import App
        app = App(window_class, self._login, self._options, self, embedded,
                  app_window, appdesc.name)

        toplevel = app.main_window.get_toplevel()
        icon = toplevel.render_icon(appdesc.icon, gtk.ICON_SIZE_MENU)
        toplevel.set_icon(icon)

        from stoqlib.gui.events import StartApplicationEvent
        StartApplicationEvent.emit(appdesc.name, app)

        return app

    def _get_available_applications(self):
        from kiwi.component import get_utility
        from stoqlib.lib.interfaces import IApplicationDescriptions
        from stoq.lib.applist import Application

        permissions = {}
        for settings in self._user.profile.profile_settings:
            permissions[settings.app_dir_name] = settings.has_permission

        descriptions = get_utility(IApplicationDescriptions).get_descriptions()

        available_applications = []

        # sorting by app_full_name
        for name, full, icon, descr in sorted(descriptions,
                                              key=operator.itemgetter(1)):
            if name in self._hidden_apps:
                continue
            if permissions.get(name) and name not in self._blocked_apps:
                available_applications.append(
                    Application(name, full, icon, descr))

        return available_applications

    def _get_current_username(self):
        from stoqlib.api import api
        conn = api.get_connection()
        user = api.get_current_user(conn)
        return user.username

    #
    # Global functions
    #

    def _write_exception_hook(self, exctype, value, tb):
        import traceback
        from stoq.gui.shell import get_shell
        from stoqlib.gui.base.dialogs import get_current_toplevel

        shell = get_shell()
        if shell:
            appname = shell.get_current_app_name()
        else:
            appname = 'unknown'

        window = get_current_toplevel()
        if window:
            window_name = window.name
        else:
            window_name = 'unknown'

        log.info('An error occurred in application "%s", toplevel window=%s:' % (
            appname, window_name))

        traceback.print_exception(exctype, value, tb, file=self._stream)

        from stoqlib.lib.crashreport import collect_traceback
        collect_traceback((exctype, value, tb))

    def _debug_hook(self, exctype, value, tb):
        import traceback
        self._write_exception_hook(exctype, value, tb)
        traceback.print_exception(exctype, value, tb)
        print
        print '-- Starting debugger --'
        print
        import pdb
        pdb.pm()

    # FIXME: this logic should be inside stoqlib.
    def _exit_func(self):
        from stoqlib.lib.daemonutils import stop_daemon
        stop_daemon()

        from stoqlib.database.runtime import get_current_user, get_connection
        from stoqlib.exceptions import StoqlibError
        from stoqlib.lib.process import Process
        try:
            user = get_current_user(get_connection())
            if user:
                user.logout()
        except StoqlibError:
            pass

        if self._cur_exit_func:
            self._cur_exit_func()

        if self._restart:
            Process([sys.argv[0]], shell=True)

    #
    # Public API
    #

    def login(self, try_cookie=True):
        """
        Do a login
        @param try_cookie: Try to use a cookie if one is available
        @returns: True if login succeed, otherwise false
        """
        from stoqlib.exceptions import LoginError
        from stoqlib.lib.message import info
        user = None
        if try_cookie:
            user = self._login.cookie_login()

        if not user:
            try:
                user = self._login.validate_user()
            except LoginError, e:
                info(e)

        if user:
            self._user = user
        return bool(user)

    def relogin(self):
        """
        Do a relogin, eg switch users
        """
        if self._current_app:
            self._current_app.hide()

        old_user = self._get_current_username()

        if not self.login(try_cookie=False):
            return

        # If the username is the same
        if (old_user == self._get_current_username() and
            self._current_app):
            self._current_app.show()
            return

        # clear the cache, since we switched users
        self._application_cache.clear()

        from stoq.gui.launcher import Launcher
        launcher = Launcher(self._options, self)
        launcher.show()

    def get_app_by_name(self, appname):
        """
        @param appname: a string
        @returns: a :class:`Application` object
        """
        from kiwi.component import get_utility
        from stoq.lib.applist import Application
        from stoqlib.lib.interfaces import IApplicationDescriptions
        descriptions = get_utility(IApplicationDescriptions).get_descriptions()
        for name, full, icon, descr in descriptions:
            if name == appname:
                return Application(name, full, icon, descr)

    def get_current_app_name(self):
        """
        Get the name of the currently running application
        @returns: the name
        @rtype: str
        """
        return self._appname

    def block_application(self, appname):
        """Blocks an application to be loaded.
        @param appname: the name of the application. Raises ValueError if the
                        application was already blocked.
        """
        if appname not in self._blocked_apps:
            self._blocked_apps.append(appname)
        else:
            raise ValueError('%s was already blocked.' % appname)

    def unblock_application(self, appname):
        """Unblocks a previously blocked application.
        @param appname: the name of the blocked application. Raises ValueError
                        if the application was not previously blocked.
        """
        if appname in self._blocked_apps:
            self._blocked_apps.remove(appname)
        else:
            raise ValueError('%s was not blocked.' % appname)

    def restart_atexit(self):
        self._restart = True

    def run(self, appdesc=None, appname=None):
        self._do_login()
        from stoq.gui.launcher import Launcher
        from stoqlib.lib.message import error
        import gtk
        app_window = Launcher(self._options, self)
        app_window.show()

        # A GtkWindowGroup controls grabs (blocking mouse/keyboard interaction),
        # by default all windows are added to the same window group.
        # We want to avoid setting modallity on other windows
        # when running a dialog using gtk_dialog_run/run_dialog.
        window_group = gtk.WindowGroup()
        window_group.add_window(app_window.get_toplevel())

        if appname is not None:
            appdesc = self.get_app_by_name(appname)

        if not appdesc:
            return
        if (appdesc.name != 'launcher' and
            not self._user.profile.check_app_permission(appdesc.name)):
            error(_("This user lacks credentials \nfor application %s") %
                  appdesc.name)
            return

        self.run_embedded(appdesc, app_window)

    def run_embedded(self, appdesc, app_window, params=None):
        app = self._load_app(appdesc, app_window)
        app.launcher = app_window

        self._current_app = app
        self._appname = appdesc.name

        if appdesc.name in self._blocked_apps:
            app_window.show()
            return

        app.run(params)

        # Possibly correct window position (livecd workaround for small
        # screens)
        from stoqlib.lib.pluginmanager import get_plugin_manager
        manager = get_plugin_manager()
        from stoqlib.api import api
        if (api.sysparam(api.get_connection()).DEMO_MODE
            and manager.is_active('ecf')):
            pos = app.main_window.toplevel.get_position()
            if pos[0] < 220:
                app.main_window.toplevel.move(220, pos[1])

        return app

    def main(self, appname):
        self._bootstrap()
        self.run(appname=appname)

        from twisted.internet import reactor
        log.debug("Entering reactor")
        reactor.run()
        log.info("Leaving reactor")


def get_shell():
    return _shell
