#!/usr/bin/env python
#
# Copyright 2012, Google Inc.
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


"""Test for end-to-end."""


import logging
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

import set_sys_path  # Update sys.path to locate mod_pywebsocket module.

from test import client_for_testing
from test import mux_client_for_testing


# Special message that tells the echo server to start closing handshake
_GOODBYE_MESSAGE = 'Goodbye'

# If you want to use external server to run end to end tests, set following
# parameters correctly.
_use_external_server = False
_external_server_port = 0


# Test body functions
def _echo_check_procedure(client):
    client.connect()

    client.send_message('test')
    client.assert_receive('test')
    client.send_message('helloworld')
    client.assert_receive('helloworld')

    client.send_close()
    client.assert_receive_close()

    client.assert_connection_closed()


def _echo_check_procedure_with_binary(client):
    client.connect()

    client.send_message('binary', binary=True)
    client.assert_receive('binary', binary=True)
    client.send_message('\x00\x80\xfe\xff\x00\x80', binary=True)
    client.assert_receive('\x00\x80\xfe\xff\x00\x80', binary=True)

    client.send_close()
    client.assert_receive_close()

    client.assert_connection_closed()


def _echo_check_procedure_with_goodbye(client):
    client.connect()

    client.send_message('test')
    client.assert_receive('test')

    client.send_message(_GOODBYE_MESSAGE)
    client.assert_receive(_GOODBYE_MESSAGE)

    client.assert_receive_close()
    client.send_close()

    client.assert_connection_closed()


def _echo_check_procedure_with_code_and_reason(client, code, reason):
    client.connect()

    client.send_close(code, reason)
    client.assert_receive_close(code, reason)

    client.assert_connection_closed()


def _unmasked_frame_check_procedure(client):
    client.connect()

    client.send_message('test', mask=False)
    client.assert_receive_close(client_for_testing.STATUS_PROTOCOL_ERROR, '')

    client.assert_connection_closed()


def _mux_echo_check_procedure(mux_client):
    mux_client.connect()
    mux_client.send_flow_control(1, 1024)

    logical_channel_options = client_for_testing.ClientOptions()
    logical_channel_options.server_host = 'localhost'
    logical_channel_options.server_port = 80
    logical_channel_options.origin = 'http://localhost'
    logical_channel_options.resource = '/echo'
    mux_client.add_channel(2, logical_channel_options)
    mux_client.send_flow_control(2, 1024)

    mux_client.send_message(2, 'test')
    mux_client.assert_receive(2, 'test')

    mux_client.add_channel(3, logical_channel_options)
    mux_client.send_flow_control(3, 1024)

    mux_client.send_message(2, 'hello')
    mux_client.send_message(3, 'world')
    mux_client.assert_receive(2, 'hello')
    mux_client.assert_receive(3, 'world')

    # Don't send close message on channel id 1 so that server-initiated
    # closing handshake won't occur.
    mux_client.send_close(2)
    mux_client.send_close(3)
    mux_client.assert_receive_close(2)
    mux_client.assert_receive_close(3)

    mux_client.send_physical_connection_close()
    mux_client.assert_physical_connection_receive_close()


class EndToEndTest(unittest.TestCase):
    """An end-to-end test that launches pywebsocket standalone server as a
    separate process, connects to it using the client_for_testing module, and
    checks if the server behaves correctly by exchanging opening handshake and
    frames over a TCP connection.
    """

    def setUp(self):
        self.server_stderr = None
        self.top_dir = os.path.join(os.path.split(__file__)[0], '..')
        os.putenv('PYTHONPATH', os.path.pathsep.join(sys.path))
        self.standalone_command = os.path.join(
            self.top_dir, 'mod_pywebsocket', 'standalone.py')
        self.document_root = os.path.join(self.top_dir, 'example')
        s = socket.socket()
        s.bind(('localhost', 0))
        (_, self.test_port) = s.getsockname()
        s.close()

        self._options = client_for_testing.ClientOptions()
        self._options.server_host = 'localhost'
        self._options.origin = 'http://localhost'
        self._options.resource = '/echo'

        # TODO(toyoshim): Eliminate launching a standalone server on using
        # external server.

        if _use_external_server:
            self._options.server_port = _external_server_port
        else:
            self._options.server_port = self.test_port

    def _run_python_command(self, commandline, stdout=None, stderr=None):
        return subprocess.Popen([sys.executable] + commandline, close_fds=True,
                                stdout=stdout, stderr=stderr)

    def _run_server(self, allow_draft75=False):
        args = [self.standalone_command,
                '-H', 'localhost',
                '-V', 'localhost',
                '-p', str(self.test_port),
                '-P', str(self.test_port),
                '-d', self.document_root]

        # Inherit the level set to the root logger by test runner.
        root_logger = logging.getLogger()
        log_level = root_logger.getEffectiveLevel()
        if log_level != logging.NOTSET:
            args.append('--log-level')
            args.append(logging.getLevelName(log_level).lower())

        if allow_draft75:
            args.append('--allow-draft75')

        return self._run_python_command(args,
                                        stderr=self.server_stderr)

    def _kill_process(self, pid):
        if sys.platform in ('win32', 'cygwin'):
            subprocess.call(
                ('taskkill.exe', '/f', '/pid', str(pid)), close_fds=True)
        else:
            os.kill(pid, signal.SIGKILL)

    def _run_hybi_test_with_client_options(self, test_function, options):
        server = self._run_server()
        try:
            # TODO(tyoshino): add some logic to poll the server until it
            # becomes ready
            time.sleep(0.2)

            client = client_for_testing.create_client(options)
            try:
                test_function(client)
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def _run_hybi_test(self, test_function):
        self._run_hybi_test_with_client_options(test_function, self._options)

    def _run_hybi_deflate_test(self, test_function):
        server = self._run_server()
        try:
            time.sleep(0.2)

            self._options.enable_deflate_stream()
            client = client_for_testing.create_client(self._options)
            try:
                test_function(client)
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def _run_hybi_deflate_frame_test(self, test_function):
        server = self._run_server()
        try:
            time.sleep(0.2)

            self._options.enable_deflate_frame()
            client = client_for_testing.create_client(self._options)
            try:
                test_function(client)
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def _run_hybi_close_with_code_and_reason_test(self, test_function, code,
                                                  reason):
        server = self._run_server()
        try:
            time.sleep(0.2)

            client = client_for_testing.create_client(self._options)
            try:
                test_function(client, code, reason)
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def _run_hybi_http_fallback_test(self, options, status):
        server = self._run_server()
        try:
            time.sleep(0.2)

            client = client_for_testing.create_client(options)
            try:
                client.connect()
                self.fail('Could not catch HttpStatusException')
            except client_for_testing.HttpStatusException, e:
                self.assertEqual(status, e.status)
            except Exception, e:
                self.fail('Catch unexpected exception')
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def _run_hybi_mux_test(self, test_function):
        server = self._run_server()
        try:
            time.sleep(0.2)

            client = mux_client_for_testing.MuxClient(self._options)
            try:
                test_function(client)
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def test_echo(self):
        self._run_hybi_test(_echo_check_procedure)

    def test_echo_binary(self):
        self._run_hybi_test(_echo_check_procedure_with_binary)

    def test_echo_server_close(self):
        self._run_hybi_test(_echo_check_procedure_with_goodbye)

    def test_unmasked_frame(self):
        self._run_hybi_test(_unmasked_frame_check_procedure)

    def test_echo_deflate(self):
        self._run_hybi_deflate_test(_echo_check_procedure)

    def test_echo_deflate_server_close(self):
        self._run_hybi_deflate_test(_echo_check_procedure_with_goodbye)

    def test_echo_deflate_frame(self):
        self._run_hybi_deflate_frame_test(_echo_check_procedure)

    def test_echo_deflate_frame_server_close(self):
        self._run_hybi_deflate_frame_test(
            _echo_check_procedure_with_goodbye)

    def test_echo_close_with_code_and_reason(self):
        self._options.resource = '/close'
        self._run_hybi_close_with_code_and_reason_test(
            _echo_check_procedure_with_code_and_reason, 3333, 'sunsunsunsun')

    def test_echo_close_with_empty_body(self):
        self._options.resource = '/close'
        self._run_hybi_close_with_code_and_reason_test(
            _echo_check_procedure_with_code_and_reason, None, '')

    def test_mux_echo(self):
        self._run_hybi_mux_test(_mux_echo_check_procedure)

    def test_close_on_protocol_error(self):
        """Tests that the server sends a close frame with protocol error status
        code when the client sends data with some protocol error.
        """

        def test_function(client):
            client.connect()

            # Intermediate frame without any preceding start of fragmentation
            # frame.
            client.send_frame_of_arbitrary_bytes('\x80\x80', '')
            client.assert_receive_close(
                client_for_testing.STATUS_PROTOCOL_ERROR)

        self._run_hybi_test(test_function)

    def test_close_on_unsupported_frame(self):
        """Tests that the server sends a close frame with unsupported operation
        status code when the client sends data asking some operation that is
        not supported by the server.
        """

        def test_function(client):
            client.connect()

            # Text frame with RSV3 bit raised.
            client.send_frame_of_arbitrary_bytes('\x91\x80', '')
            client.assert_receive_close(
                client_for_testing.STATUS_UNSUPPORTED_DATA)

        self._run_hybi_test(test_function)

    def test_close_on_invalid_frame(self):
        """Tests that the server sends a close frame with invalid frame payload
        data status code when the client sends an invalid frame like containing
        invalid UTF-8 character.
        """

        def test_function(client):
            client.connect()

            # Text frame with invalid UTF-8 string.
            client.send_message('\x80', raw=True)
            client.assert_receive_close(
                client_for_testing.STATUS_INVALID_FRAME_PAYLOAD_DATA)

        self._run_hybi_test(test_function)

    def _run_hybi00_test(self, test_function):
        server = self._run_server()
        try:
            time.sleep(0.2)

            client = client_for_testing.create_client_hybi00(self._options)
            try:
                test_function(client)
            finally:
                client.close_socket()
        finally:
            self._kill_process(server.pid)

    def test_echo_hybi00(self):
        self._run_hybi00_test(_echo_check_procedure)

    def test_echo_server_close_hybi00(self):
        self._run_hybi00_test(_echo_check_procedure_with_goodbye)

    # TODO(toyoshim): Add tests to verify invalid absolute uri handling like
    # host unmatch, port unmatch and invalid port description (':' without port
    # number).

    def test_absolute_uri(self):
        """Tests absolute uri request."""

        options = self._options
        options.resource = 'ws://localhost:%d/echo' % options.server_port
        self._run_hybi_test_with_client_options(_echo_check_procedure, options)

    def test_origin_check(self):
        """Tests http fallback on origin check fail."""

        options = self._options
        options.resource = '/origin_check'
        # Server shows warning message for http 403 fallback. This warning
        # message is confusing. Following pipe disposes warning messages.
        self.server_stderr = subprocess.PIPE
        self._run_hybi_http_fallback_test(options, 403)

    def test_version_check(self):
        """Tests http fallback on version check fail."""

        options = self._options
        options.version = 99
        self.server_stderr = subprocess.PIPE
        self._run_hybi_http_fallback_test(options, 400)

    def _check_example_echo_client_result(
        self, expected, stdoutdata, stderrdata):
        actual = stdoutdata.decode("utf-8")
        if actual != expected:
            raise Exception('Unexpected result on example echo client: '
                            '%r (expected) vs %r (actual)' %
                            (expected, actual))
        if stderrdata is not None:
            raise Exception('Unexpected error message on example echo '
                            'client: %r' % stderrdata)

    def test_example_echo_client(self):
        """Tests that the echo_client.py example can talk with the server."""

        server = self._run_server()
        try:
            time.sleep(0.2)

            client_command = os.path.join(
                self.top_dir, 'example', 'echo_client.py')

            args = [client_command,
                    '-p', str(self._options.server_port)]
            client = self._run_python_command(args, stdout=subprocess.PIPE)
            stdoutdata, stderrdata = client.communicate()
            expected = ('Send: Hello\n' 'Recv: Hello\n'
                u'Send: \u65e5\u672c\n' u'Recv: \u65e5\u672c\n'
                'Send close\n' 'Recv ack\n')
            self._check_example_echo_client_result(
                expected, stdoutdata, stderrdata)

            # Process a big message for which extended payload length is used.
            # To handle extended payload length, ws_version attribute will be
            # accessed. This test checks that ws_version is correctly set.
            big_message = 'a' * 1024
            args = [client_command,
                    '-p', str(self._options.server_port),
                    '-m', big_message]
            client = self._run_python_command(args, stdout=subprocess.PIPE)
            stdoutdata, stderrdata = client.communicate()
            expected = ('Send: %s\nRecv: %s\nSend close\nRecv ack\n' %
                        (big_message, big_message))
            self._check_example_echo_client_result(
                expected, stdoutdata, stderrdata)
        finally:
            self._kill_process(server.pid)


if __name__ == '__main__':
    unittest.main()


# vi:sts=4 sw=4 et
