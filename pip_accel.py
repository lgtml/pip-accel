#!/usr/bin/env python

# Accelerator for pip, the Python package manager.
#
# Author: Peter Odding <peter.odding@paylogic.eu>
# Last Change: April 11, 2013
# URL: https://github.com/paylogic/pip-accel

"""
Usage: pip-accel [ARGUMENTS TO PIP]

The pip-accel program is a wrapper for pip, the Python package manager. It
accelerates the usage of pip to initialize Python virtual environments given
one or more requirements files. The pip-accel command supports all subcommands
and options supported by pip, however it is of course only useful for the "pip
install" subcommand.

For more information please refer to the GitHub project page:
https://github.com/paylogic/pip-accel
"""

import os
import os.path
import re
import shutil
import sys
import tarfile
import time
import urllib
import urlparse

# Whether a user is directly looking at the output.
INTERACTIVE = os.isatty(1)

# Check if the operator requested verbose output.
VERBOSE = '-v' in sys.argv

# The main loop of pip-accel retries at most this many times to counter pip errors
# due to connectivity issues with PyPi and/or linked distribution websites.
MAX_RETRIES = 10

# Select the location of the download cache and other files based on the user
# running the pip-accel command (root goes to /var/lib, otherwise ~/.pip-accel).
if os.getuid() == 0:
    download_cache = '/root/.pip/download-cache'
    source_index = '/var/lib/pip-accel/sources'
    binary_index = '/var/lib/pip-accel/binaries'
else:
    download_cache = os.path.expanduser(os.environ.get('PIP_DOWNLOAD_CACHE', '~/.pip/download-cache'))
    source_index = os.path.expanduser('~/.pip-accel/sources')
    binary_index = os.path.expanduser('~/.pip-accel/binaries')

def main():
    """
    Main logic of the pip-accel command.
    """
    main_timer = Timer()
    original_arguments = sys.argv[1:]
    # If no install subcommand is given we pass the command line straight
    # to pip without any changes and exit immediately afterwards.
    if 'install' not in original_arguments:
        sys.exit(os.spawnvp(os.P_WAIT, 'pip', ['pip'] + original_arguments))
    # Create all required directories on the fly.
    for directory in [download_cache, source_index, binary_index]:
        if not os.path.isdir(directory):
            os.makedirs(directory)
    # Execute "pip install" in a loop in order to retry after intermittent
    # error responses from servers (which can happen quite frequently).
    for i in xrange(1, MAX_RETRIES):
        have_source_dists, dependencies = unpack_source_dists(original_arguments)
        if not have_source_dists:
            download_source_dists(original_arguments)
        elif not dependencies:
            message("No dependencies found in pip's output, probably there's nothing to do.\n")
            return
        else:
            if build_binary_dists(dependencies) and install_dependencies(dependencies):
                message("Done! Took %s to install %i packages.\n", main_timer, len(dependencies))
            return
    message("External command failed %i times, aborting!\n" % MAX_RETRIES)
    sys.exit(1)

def unpack_source_dists(original_arguments):
    """
    Check whether we have local source distributions available for all
    dependencies and find the names and versions of all dependencies.
    """
    unpack_timer = Timer()
    message("Unpacking local source distributions ..\n")
    # Create a shallow copy of the original argument
    # list that includes the --no-install option.
    instance_arguments = [a for a in original_arguments]
    instance_arguments.append('--no-install')
    # Execute pip to unpack the source distributions.
    # FIXME This doesn't support "pip install --editable" yet!
    status, output = run_pip(['-v', '-v'] + instance_arguments,
                             local_index=source_index,
                             use_remote_index=False)
    # If pip failed, notify the user.
    if not status:
        interactive_message("Warning: We probably don't have all source distributions yet")
        return False, None
    message("Unpacked local source distributions in %s.\n", unpack_timer)
    # If pip succeeded, parse its output to find the pinned dependencies.
    dependencies = []
    pattern = re.compile(r'^\s*Source in (.+?) has version (.+?), which satisfies requirement')
    for line in output:
        m = pattern.match(line)
        if m:
            directory = m.group(1)
            name = os.path.basename(directory)
            version = m.group(2)
            dependencies.append((name, version, directory))
    message("Found %i dependenc%s in pip's output%s\n",
            len(dependencies),
            len(dependencies) == 1 and 'y' or 'ies',
            len(dependencies) >= 1 and VERBOSE and ':' or '.')
    if dependencies and VERBOSE:
        for name, version, directory in dependencies:
            message(" - %s (%s)\n", name, version)
    return True, dependencies

def download_source_dists(original_arguments):
    """
    Download missing source distributions.
    """
    download_timer = Timer()
    message("Downloading source distributions ..\n")
    # Create a shallow copy of the original argument
    # list that includes the --no-install option.
    instance_arguments = [a for a in original_arguments]
    instance_arguments.append('--no-install')
    # Execute pip to download missing source distributions.
    status, output = run_pip(instance_arguments,
                             local_index=source_index,
                             use_remote_index=True)
    if status:
        message("Finished downloading source distributions in %s.\n", download_timer)
    # If pip failed, notify the user.
    if not status:
        interactive_message("Warning: Failed to download one or more source distributions")

def find_binary_dists():
    """
    Find cached binary distributions.
    """
    message("Scanning binary distribution index ..\n")
    distributions = {}
    for filename in sorted(os.listdir(binary_index), key=str.lower):
        if filename.endswith('.tar.gz'):
            # Created with: python setup.py bdist
            #  - chardet 2.1.1 => chardet-2.1.1.linux-x86_64.tar.gz
            #  - MySQL-python 1.2.3 => MySQL-python-1.2.3.linux-x86_64.tar.gz
            # FIXME Extend this to support at least 32-bit and other Unixen?
            m = re.match(r'^([A-Za-z].*)-([0-9].*?)\.linux-x86_64\.tar\.gz$', filename)
            if m:
                pathname = os.path.join(binary_index, filename)
                key = (m.group(1), m.group(2))
                if VERBOSE:
                    message("Matched %s in %s\n", key, filename)
                distributions[key] = pathname
                continue
        message("Failed to match filename: %s\n", filename)
    message("Found %i existing binary distributions%s\n", len(distributions), VERBOSE and ':' or '.')
    if VERBOSE:
        for (name, version), filename in distributions.iteritems():
            message(" - %s (%s) in %s\n", name, version, filename)
    return distributions

def build_binary_dists(dependencies):
    """
    Convert source distributions to binary distributions.
    """
    existing_binary_dists = find_binary_dists()
    message("Building binary distributions ..\n")
    for name, version, directory in dependencies:
        # Check if a binary distribution already exists.
        filename = existing_binary_dists.get((name, version))
        if filename:
            if VERBOSE:
                message("Existing binary distribution for %s (%s) found at %s\n", name, version, filename)
            continue
        # Make sure the source distribution contains a setup script.
        setup_script = os.path.join(directory, 'setup.py')
        if not os.path.isfile(setup_script):
            message("Warning: Package %s (%s) is not a source distribution?!\n", name, version)
        else:
            old_directory = os.getcwd()
            # Build a binary distribution.
            os.chdir(directory)
            message("Building binary distribution of %s (%s) ..\n", name, version)
            status = (os.system('python setup.py bdist') == 0)
            os.chdir(old_directory)
            # Move the generated distribution to the binary index.
            if not status:
                message("Failed to build binary distribution!\n")
                return False
            else:
                filenames = os.listdir(os.path.join(directory, 'dist'))
                if not filenames:
                    message("Error: Build process did not result in a binary distribution!\n")
                    return False
                for filename in filenames:
                    message("Copying binary distribution to cache: %s\n", filename)
                    shutil.move(os.path.join(directory, 'dist', filename),
                            os.path.join(binary_index, filename))
    message("Finished building binary distributions.\n")
    return True

def install_dependencies(dependencies):
    """
    Manually install all dependencies from binary distributions.
    """
    install_timer = Timer()
    existing_binary_dists = find_binary_dists()
    message("Installing from binary distributions ..\n")
    for name, version, directory in dependencies:
        # FIXME Make this case insensitive!
        filename = existing_binary_dists.get((name, version))
        if not filename:
            message("Error: No binary distribution of %s (%s) available!\n", name, version)
            return False
        install_binary_dist(filename)
    message("Finished installing all dependencies in %s.\n", install_timer)
    return True

def install_binary_dist(filename, install_prefix=sys.prefix):
    """
    Install a binary distribution created with `python setup.py bdist` into the
    given prefix (a directory like /usr, /usr/local or a virtual environment).
    """
    install_timer = Timer()
    python = os.path.join(install_prefix, 'bin', 'python')
    message("Installing binary distribution %s to %s ..\n", filename, install_prefix)
    archive = tarfile.open(filename, 'r:gz')
    for original_path, relative_path, mode in find_bdist_contents(archive):
        install_path = os.path.join(install_prefix, relative_path)
        directory = os.path.dirname(install_path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        if VERBOSE:
            message("Writing %s\n", install_path)
        file_handle = archive.extractfile(original_path)
        with open(install_path, 'w') as handle:
            contents = file_handle.read()
            if contents.startswith('#!/'):
                # Fix hashbangs.
                contents = re.sub('^#![^\n]+', '#!' + python, contents)
            handle.write(contents)
        os.chmod(install_path, mode)
    archive.close()
    message("Finished installing binary distribution in %s.\n", install_timer)

def find_bdist_contents(archive):
    """
    Transform the absolute pathnames embedded in a binary distribution into
    relative filenames that can be prefixed by /usr, /usr/local or the path to
    a virtual environment.
    """
    contents = []
    while True:
        member = archive.next()
        if not member:
            break
        original_path = member.name
        member_is_file = member.isfile()
        for substring in ['lib', 'bin', 'man']:
            tokens = original_path.split('/%s/' % substring)
            if len(tokens) >= 2:
                relative_path = os.path.join(substring, tokens[-1])
                if relative_path != original_path and member_is_file:
                    contents.append((original_path, relative_path, member.mode))
                    break
        else:
            if member_is_file:
                message("Warning: Ignoring unmatched file %s\n", original_path)
    return contents

def interactive_message(text):
    """
    Show a message to the operator for 5 seconds.
    """
    i = 5
    while i >= 1:
        message("%s, retrying after %i %s .. ", text, i, i == 1 and 'second' or 'seconds')
        time.sleep(1)
        i -= 1
    message("\n", prefix=False)

def run_pip(arguments, local_index, use_remote_index):
    """
    Execute a modified `pip install` command.
    """
    command_line = []
    for i, arg in enumerate(arguments):
        if arg == 'install':
            command_line += ['pip'] + arguments[:i+1] + [
                    '--download-cache=%s' % download_cache,
                    '--find-links=file://%s' % local_index]
            if not use_remote_index:
                command_line += ['--no-index']
            command_line += arguments[i+1:]
            break
    else:
        command_line += ['pip'] + arguments
    message("Executing command: %s\n", ' '.join(command_line))
    pip = os.popen(' '.join(command_line))
    output = []
    for line in pip:
        message("  %s\n", line.rstrip(), prefix=False)
        output.append(line)
    if pip.close() is None:
        update_source_dists_index()
        return True, output
    else:
        return False, None

def update_source_dists_index():
    """
    Link newly downloaded source distributions into the local index directory.
    """
    for download_name in os.listdir(download_cache):
        download_path = os.path.join(download_cache, download_name)
        url = urllib.unquote(download_name)
        if not url.endswith('.content-type'):
            components = urlparse.urlparse(url)
            archive_name = os.path.basename(components.path)
            archive_path = os.path.join(source_index, add_extension(download_path, archive_name))
            if not os.path.isfile(archive_path):
                message("Linking files:\n")
                message(" - Source: %s\n", download_path)
                message(" - Target: %s\n", archive_path)
                os.symlink(download_path, archive_path)

def add_extension(download_path, archive_path):
    """
    Make sure all cached source distributions have the right file extension.
    """
    handle = open(download_path)
    header = handle.read(2)
    handle.close()
    if header.startwith('\x1f\x8b'):
        # The gzip compression header is two bytes: 0x1F, 0x8B.
        if not archive_path.endswith(('.tgz', '.tar.gz')):
            archive_path += '.tar.gz'
    elif header.startswith('BZ'):
        # The bzip2 compression header is two bytes: B, Z.
        if not archive_path.endswith('.bz2'):
            archive_path += '.bz2'
    elif header.startswith('PK'):
        # According to Wikipedia, ZIP archives don't have an official magic
        # number, but most of the time we'll find two bytes: P, K (for Phil
        # Katz, creator of the format).
        if not archive_path.endswith('.zip'):
            archive_path += '.zip'
    return archive_path

def message(text, *args, **kw):
    """
    Print a formatted message to the console.
    """
    if kw.get('prefix', True):
        text = '(pip-accel) ' + text
    if INTERACTIVE:
        text = '\r' + text
    sys.stderr.write(text % args)

class Timer:

    """
    Easy to use timer to keep track of long during operations.
    """

    def __init__(self):
        self.start_time = time.time()

    def __str__(self):
        return "%.2f seconds" % self.elapsed_time

    @property
    def elapsed_time(self):
        return time.time() - self.start_time

if __name__ == '__main__':
    main()

# vim: ft=python ts=4 sw=4 et