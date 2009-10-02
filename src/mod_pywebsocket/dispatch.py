# Copyright 2009, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


"""Dispatch Web Socket request.
"""


import os
import re

import util


_SOURCE_PATH_PATTERN = re.compile(r'(?i)_wsh\.py$')
_SOURCE_SUFFIX = '_wsh.py'
_SHAKE_HANDS_HANDLER_NAME = 'web_socket_shake_hands'
_TRANSFER_DATA_HANDLER_NAME = 'web_socket_transfer_data'


class DispatchError(Exception):
    """Exception in dispatching Web Socket request."""

    pass


def _path_to_resource_converter(base_dir):
    base_dir = os.path.normpath(base_dir).replace('\\', '/')
    base_len = len(base_dir)
    suffix_len = len(_SOURCE_SUFFIX)
    def converter(path):
        if not path.endswith(_SOURCE_SUFFIX):
            return None
        path = os.path.normpath(path).replace('\\', '/')
        if not path.startswith(base_dir):
            return None
        return path[base_len:-suffix_len]
    return converter


def _source_file_paths(directory):
    """Yield Web Socket Handler source file names in the given directory."""

    for root, unused_dirs, files in os.walk(directory):
        for base in files:
            path = os.path.join(root, base)
            if _SOURCE_PATH_PATTERN.search(path):
                yield path


def _source(source_str):
    """Source a handler definition string."""

    global_dic = {}
    try:
        exec source_str in global_dic
    except Exception:
        raise DispatchError('Error in sourcing handler:' +
                            util.get_stack_trace())
    return (_extract_handler(global_dic, _SHAKE_HANDS_HANDLER_NAME),
            _extract_handler(global_dic, _TRANSFER_DATA_HANDLER_NAME))


def _extract_handler(dic, name):
    if name not in dic:
        raise DispatchError('%s is not defined.' % name)
    handler = dic[name]
    if not callable(handler):
        raise DispatchError('%s is not callable.' % name)
    return handler


class Dispatcher(object):
    """Dispatches Web Socket requests.

    This class maintains a map from resource name to handlers.
    """

    def __init__(self, root_dir):
        """Construct an instance.

        Args:
            root_dir: The directory where handler definition files are
            placed.
        """

        self._handlers = {}
        self._source_warnings = []
        self._source_files_in_dir(root_dir)

    def source_warnings(self):
        """Return warnings in sourcing handlers."""

        return self._source_warnings

    def shake_hands(self, request):
        """Hook into Web Socket handshake.

        Select a handler based on request.uri and call its
        web_socket_shake_hands function.

        Args:
            request: mod_python request.
        """

        shake_hands_, _ = self._handler(request)
        try:
            shake_hands_(request)
        except Exception:
            raise DispatchError('shake_hands() raised exception: ' +
                                util.get_stack_trace())

    def transfer_data(self, request):
        """Let a handler transfer_data with a Web Socket client.

        Select a handler based on request.ws_resource and call its
        web_socket_transfer_data function.

        Args:
            request: mod_python request.
        """

        _, transfer_data_ = self._handler(request)
        try:
            transfer_data_(request)
        except Exception:
            raise DispatchError('transfer_data() raised exception: ' +
                                util.get_stack_trace())

    def _handler(self, request):
        try:
            return self._handlers[request.ws_resource]
        except KeyError:
            raise DispatchError('No handler for: %r' % request.ws_resource)

    def _source_files_in_dir(self, root_dir):
        """Source all the handler source files in the directory."""

        to_resource = _path_to_resource_converter(root_dir)
        for path in _source_file_paths(root_dir):
            try:
                handler = _source(open(path).read())
            except DispatchError, e:
                self._source_warnings.append('%s: %s' % (path, e))
                continue
            self._handlers[to_resource(path)] = handler


# vi:sts=4 sw=4 et
