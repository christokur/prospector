import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import List

from pylint.config import find_pylintrc
from pylint.exceptions import UnknownMessageError
from pylint.lint.run import _cpu_count

from prospector.finder import FileFinder
from prospector.message import Location, Message
from prospector.tools.base import ToolBase
from prospector.tools.pylint.collector import Collector
from prospector.tools.pylint.linter import ProspectorLinter

_UNUSED_WILDCARD_IMPORT_RE = re.compile(r"^Unused import(\(s\))? (.*) from wildcard import")


class PylintTool(ToolBase):
    # There are several methods on this class which could technically
    # be functions (they don't use the 'self' argument) but that would
    # make this module/class a bit ugly.
    # pylint:disable=no-self-use

    def __init__(self):
        self._args = None
        self._collector = self._linter = None
        self._orig_sys_path = []

    def _prospector_configure(self, prospector_config, linter: ProspectorLinter):
        errors = []

        if "django" in prospector_config.libraries:
            linter.load_plugin_modules(["pylint_django"])
        if "celery" in prospector_config.libraries:
            linter.load_plugin_modules(["pylint_celery"])
        if "flask" in prospector_config.libraries:
            linter.load_plugin_modules(["pylint_flask"])

        profile_path = os.path.join(prospector_config.workdir, prospector_config.profile.name)
        for plugin in prospector_config.profile.pylint.get("load-plugins", []):
            try:
                linter.load_plugin_modules([plugin])
            except ImportError:
                errors.append(self._error_message(profile_path, f"Could not load plugin {plugin}"))

        for msg_id in prospector_config.get_disabled_messages("pylint"):
            try:
                linter.disable(msg_id)
            except UnknownMessageError:
                # If the msg_id doesn't exist in PyLint any more,
                # don't worry about it.
                pass

        options = prospector_config.tool_options("pylint")

        for checker in linter.get_checkers():
            if not hasattr(checker, "options"):
                continue
            for option in checker.options:
                if option[0] in options:
                    checker.set_option(option[0], options[option[0]])

        # The warnings about disabling warnings are useful for figuring out
        # with other tools to suppress messages from. For example, an unused
        # import which is disabled with 'pylint disable=unused-import' will
        # still generate an 'FL0001' unused import warning from pyflakes.
        # Using the information from these messages, we can figure out what
        # was disabled.
        linter.disable("locally-disabled")  # notification about disabling a message
        linter.enable("file-ignored")  # notification about disabling an entire file
        linter.enable("suppressed-message")  # notification about a message being suppressed
        linter.disable("deprecated-pragma")  # notification about use of deprecated 'pragma' option

        max_line_length = prospector_config.max_line_length
        for checker in linter.get_checkers():
            if not hasattr(checker, "options"):
                continue
            for option in checker.options:
                if max_line_length is not None:
                    if option[0] == "max-line-length":
                        checker.set_option("max-line-length", max_line_length)
        return errors

    def _error_message(self, filepath, message):
        location = Location(filepath, None, None, 0, 0)
        return Message("prospector", "config-problem", location, message)

    def _pylintrc_configure(self, pylintrc, linter):
        errors = []
        are_plugins_loaded = linter.config_from_file(pylintrc)
        if not are_plugins_loaded and hasattr(linter.config, "load_plugins"):
            for plugin in linter.config.load_plugins:
                try:
                    linter.load_plugin_modules([plugin])
                except ImportError:
                    errors.append(self._error_message(pylintrc, f"Could not load plugin {plugin}"))
        return errors

    def configure(self, prospector_config, found_files: FileFinder):

        extra_sys_path = found_files.make_syspath()

        check_paths = found_files.python_packages + found_files.python_modules

        pylint_options = prospector_config.tool_options("pylint")
        self._set_path_finder(extra_sys_path, pylint_options)

        linter = ProspectorLinter(found_files)

        config_messages, configured_by = self._get_pylint_configuration(
            check_paths, linter, prospector_config, pylint_options
        )

        # we don't want similarity reports right now
        linter.disable("similarities")

        # use the collector 'reporter' to simply gather the messages
        # given by PyLint
        self._collector = Collector(linter.msgs_store)
        linter.set_reporter(self._collector)
        if linter.config.jobs == 0:
            linter.config.jobs = _cpu_count()
        self._linter = linter
        return configured_by, config_messages

    def _set_path_finder(self, extra_sys_path: List[Path], pylint_options):
        # insert the target path into the system path to get correct behaviour
        self._orig_sys_path = sys.path
        if not pylint_options.get("use_pylint_default_path_finder"):
            # note: we prepend, so that modules are preferentially found in the
            # path given as an argument. This prevents problems where we are
            # checking a module which is already on sys.path before this
            # manipulation - for example, if we are checking 'requests' in a local
            # checkout, but 'requests' is already installed system wide, pylint
            # will discover the system-wide modules first if the local checkout
            # does not appear first in the path
            sys.path = list(set([str(path.absolute()) for path in extra_sys_path] + sys.path))

    def _get_pylint_check_paths(self, found_files):
        # create a list of packages, but don't include packages which are
        # subpackages of others as checks will be duplicated
        packages = [os.path.split(p) for p in found_files.iter_package_paths(abspath=False)]
        packages.sort(key=len)
        check_paths = set()
        for package in packages:
            package_path = os.path.join(*package)
            if len(package) == 1:
                check_paths.add(package_path)
                continue
            for i in range(1, len(package)):
                if os.path.join(*package[:-i]) in check_paths:
                    break
            else:
                check_paths.add(package_path)
        for filepath in found_files.iter_module_paths(abspath=False):
            package = os.path.dirname(filepath).split(os.path.sep)
            for i in range(0, len(package)):
                if os.path.join(*package[: i + 1]) in check_paths:
                    break
            else:
                check_paths.add(filepath)
        check_paths = [found_files.to_absolute_path(p) for p in check_paths]
        return check_paths

    def _get_pylint_configuration(
        self, check_paths: List[Path], linter: ProspectorLinter, prospector_config, pylint_options
    ):
        self._args = linter.load_command_line_configuration(str(path) for path in check_paths)
        linter.load_default_plugins()

        config_messages = self._prospector_configure(prospector_config, linter)
        configured_by = None

        if prospector_config.use_external_config("pylint"):
            # try to find a .pylintrc
            pylintrc = pylint_options.get("config_file")
            external_config = prospector_config.external_config_location("pylint")
            pylintrc = pylintrc or external_config or find_pylintrc()
            if pylintrc is None:  # nothing explicitly configured
                for possible in (".pylintrc", "pylintrc", "pyproject.toml", "setup.cfg"):
                    pylintrc_path = os.path.join(prospector_config.workdir, possible)
                    # TODO: pyproject and setup.cfg might not actually have any pylint config
                    #       in, they should be skipped in that case
                    if os.path.exists(pylintrc_path):
                        pylintrc = pylintrc_path
                        break

            if pylintrc is not None:
                # load it!
                configured_by = pylintrc
                config_messages += self._pylintrc_configure(pylintrc, linter)

        return config_messages, configured_by

    def _combine_w0614(self, messages):
        """
        For the "unused import from wildcard import" messages,
        we want to combine all warnings about the same line into
        a single message.
        """
        by_loc = defaultdict(list)
        out = []

        for message in messages:
            if message.code == "unused-wildcard-import":
                by_loc[message.location].append(message)
            else:
                out.append(message)

        for location, message_list in by_loc.items():
            names = []
            for msg in message_list:
                names.append(_UNUSED_WILDCARD_IMPORT_RE.match(msg.message).group(1))

            msgtxt = "Unused imports from wildcard import: %s" % ", ".join(names)
            combined_message = Message("pylint", "unused-wildcard-import", location, msgtxt)
            out.append(combined_message)

        return out

    def combine(self, messages):
        """
        Combine repeated messages.

        Some error messages are repeated, causing many errors where
        only one is strictly necessary.

        For example, having a wildcard import will result in one
        'Unused Import' warning for every unused import.
        This method will combine these into a single warning.
        """
        combined = self._combine_w0614(messages)
        return sorted(combined)

    def run(self, found_files) -> List[Message]:
        self._linter.check(self._args)
        sys.path = self._orig_sys_path

        messages = self._collector.get_messages()
        return self.combine(messages)
