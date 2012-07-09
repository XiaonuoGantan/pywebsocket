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


"""This file provides classes and helper functions for multiplexing extension.

Specification:
http://tools.ietf.org/html/draft-ietf-hybi-websocket-multiplexing-03
"""


import collections
import copy
import email
import email.parser
import logging
import math
import struct
import threading
import traceback

from mod_pywebsocket import common
from mod_pywebsocket import handshake
from mod_pywebsocket import util
from mod_pywebsocket._stream_base import BadOperationException
from mod_pywebsocket._stream_base import ConnectionTerminatedException
from mod_pywebsocket._stream_hybi import Frame
from mod_pywebsocket._stream_hybi import Stream
from mod_pywebsocket._stream_hybi import StreamOptions
from mod_pywebsocket._stream_hybi import create_binary_frame
from mod_pywebsocket._stream_hybi import create_closing_handshake_body
from mod_pywebsocket._stream_hybi import create_header
from mod_pywebsocket._stream_hybi import parse_frame
from mod_pywebsocket.handshake import hybi


_CONTROL_CHANNEL_ID = 0
_DEFAULT_CHANNEL_ID = 1

_MUX_OPCODE_ADD_CHANNEL_REQUEST = 0
_MUX_OPCODE_ADD_CHANNEL_RESPONSE = 1
_MUX_OPCODE_FLOW_CONTROL = 2
_MUX_OPCODE_DROP_CHANNEL = 3
_MUX_OPCODE_NEW_CHANNEL_SLOT = 4

_MAX_CHANNEL_ID = 2 ** 29 - 1

# We need only these status code for now.
_HTTP_BAD_RESPONSE_MESSAGES = {
    common.HTTP_STATUS_BAD_REQUEST: 'Bad Request',
}


class MuxUnexpectedException(Exception):
    """Exception in handling multiplexing extension."""
    pass


# Temporary
class MuxNotImplementedException(Exception):
    """Raised when a flow enters unimplemented code path."""
    pass


class InvalidMuxFrameException(Exception):
    """Raised when an invalid multiplexed frame received."""
    pass


class InvalidMuxControlBlockException(Exception):
    """Raised when an invalid multiplexing control block received."""
    pass


class LogicalConnectionClosedException(Exception):
    """Raised when logical connection is gracefully closed."""
    pass


def _encode_channel_id(channel_id):
    if channel_id < 0:
        raise ValueError('Channel id %d must not be negative' % channel_id)

    if channel_id < 2 ** 7:
        return chr(channel_id)
    if channel_id < 2 ** 14:
        return struct.pack('!H', 0x8000 + channel_id)
    if channel_id < 2 ** 21:
        first = chr(0xc0 + (channel_id >> 16))
        return first + struct.pack('!H', channel_id & 0xffff)
    if channel_id < 2 ** 29:
        return struct.pack('!L', 0xe0000000 + channel_id)

    raise ValueError('Channel id %d is too large' % channel_id)


def _create_control_block_length_value(channel_id, opcode, flags, value):
    """Creates a control block that consists of objective channel id, opcode,
    flags, encoded length of opcode specific value, and the value.
    Most of control blocks have this structure.

    Args:
        channel_id: objective channel id.
        opcode: opcode of the control block.
        flags: 3bit opcode specific flags.
        value: opcode specific data.
    """

    if channel_id < 0 or channel_id > _MAX_CHANNEL_ID:
        raise ValueError('Invalid channel id: %d' % channel_id)
    if (opcode != _MUX_OPCODE_ADD_CHANNEL_REQUEST and
        opcode != _MUX_OPCODE_ADD_CHANNEL_RESPONSE and
        opcode != _MUX_OPCODE_DROP_CHANNEL):
        raise ValueError('Invalid opcode: %d' % opcode)
    if flags < 0 or flags > 7:
        raise ValueError('Invalid flags: %x' % flags)
    length = len(value)
    if length < 0 or length > 2 ** 32 - 1:
        raise ValueError('Invalid length: %d' % length)

    # The first byte consists of opcode, opcode specific flags, and size of
    # the size of value in bytes minus 1.
    if length > 0:
        # Calculate the minimum number of bits that are required to store the
        # value of length.
        bits_of_length = int(math.floor(math.log(length, 2)))
        first_byte = (opcode << 5) | (flags << 2) | (bits_of_length / 8)
    else:
        first_byte = (opcode << 5) | (flags << 2) | 0

    encoded_length = ''
    if length < 2 ** 8:
        encoded_length = chr(length)
    elif length < 2 ** 16:
        encoded_length = struct.pack('!H', length)
    elif length < 2 ** 24:
        encoded_length = chr(length >> 16) + struct.pack('!H',
                                                         length & 0xffff)
    else:
        encoded_length = struct.pack('!L', length)

    return (chr(first_byte) + _encode_channel_id(channel_id) +
            encoded_length + value)


def _create_add_channel_response(channel_id, encoded_handshake,
                                 encoding=0, rejected=False,
                                 outer_frame_mask=False):
    if encoding != 0 and encoding != 1:
        raise ValueError('Invalid encoding %d' % encoding)

    flags = (rejected << 2) | encoding
    block = _create_control_block_length_value(
                channel_id, _MUX_OPCODE_ADD_CHANNEL_RESPONSE,
                flags, encoded_handshake)
    payload = _encode_channel_id(_CONTROL_CHANNEL_ID) + block
    return create_binary_frame(payload, mask=outer_frame_mask)


def _create_drop_channel(channel_id, reason='', mux_error=False,
                         outer_frame_mask=False):
    if not mux_error and len(reason) > 0:
        raise ValueError('Reason must be empty if mux_error is False')

    flags = mux_error << 2
    block = _create_control_block_length_value(
                channel_id, _MUX_OPCODE_DROP_CHANNEL,
                flags, reason)
    payload = _encode_channel_id(_CONTROL_CHANNEL_ID) + block
    return create_binary_frame(payload, mask=outer_frame_mask)


def _parse_request_text(request_text):
    request_line, header_lines = request_text.split('\r\n', 1)

    words = request_line.split(' ')
    if len(words) != 3:
        raise ValueError('Bad Request-Line syntax %r' % request_line)
    [command, path, version] = words
    if version != 'HTTP/1.1':
        raise ValueError('Bad request version %r' % version)

    # email.parser.Parser() parses RFC 2822 (RFC 822) style headers.
    # RFC 6455 refers RFC 2616 for handshake parsing, and RFC 2616 refers
    # RFC 822.
    headers = email.parser.Parser().parsestr(header_lines)
    return command, path, version, headers


class _ControlBlock(object):
    """A structure that holds parsing result of multiplexing control block.
    Control block specific attributes will be added by _MuxFramePayloadParser.
    (e.g. encoded_handshake will be added for AddChannelRequest and
    AddChannelResponse)
    """

    def __init__(self, opcode):
        self.opcode = opcode


class _MuxFramePayloadParser(object):
    """A class that parses multiplexed frame payload."""

    def __init__(self, payload):
        self._data = payload
        self._read_position = 0
        self._logger = util.get_class_logger(self)

    def read_channel_id(self):
        """Reads channel id.

        Raises:
            InvalidMuxFrameException: when the payload doesn't contain
                valid channel id.
        """

        remaining_length = len(self._data) - self._read_position
        pos = self._read_position
        if remaining_length == 0:
            raise InvalidMuxFrameException('No channel id found')

        channel_id = ord(self._data[pos])
        channel_id_length = 1
        if channel_id & 0xe0 == 0xe0:
            if remaining_length < 4:
                raise InvalidMuxFrameException(
                    'Invalid channel id format')
            channel_id = struct.unpack('!L',
                                       self._data[pos:pos+4])[0] & 0x1fffffff
            channel_id_length = 4
        elif channel_id & 0xc0 == 0xc0:
            if remaining_length < 3:
                raise InvalidMuxFrameException(
                    'Invalid channel id format')
            channel_id = (((channel_id & 0x1f) << 16) +
                          struct.unpack('!H', self._data[pos+1:pos+3])[0])
            channel_id_length = 3
        elif channel_id & 0x80 == 0x80:
            if remaining_length < 2:
                raise InvalidMuxFrameException(
                    'Invalid channel id format')
            channel_id = struct.unpack('!H',
                                       self._data[pos:pos+2])[0] & 0x3fff
            channel_id_length = 2
        self._read_position += channel_id_length

        return channel_id

    def read_inner_frame(self):
        """Reads an inner frame.

        Raises:
            InvalidMuxFrameException: when the inner frame is invalid.
        """

        if len(self._data) == self._read_position:
            raise InvalidMuxFrameException('No inner frame bits found')
        bits = ord(self._data[self._read_position])
        self._read_position += 1
        fin = (bits & 0x80) == 0x80
        rsv1 = (bits & 0x40) == 0x40
        rsv2 = (bits & 0x20) == 0x20
        rsv3 = (bits & 0x10) == 0x10
        opcode = bits & 0xf
        payload = self.remaining_data()
        # Consume rest of the message which is payload data of the original
        # frame.
        self._read_position = len(self._data)
        header = create_header(opcode, len(payload), fin, rsv1, rsv2, rsv3,
                               mask=False)
        return header + payload

    def _read_opcode_specific_data(self, opcode, size_of_size):
        """Reads opcode specific data that consists of followings:
            - the size of the opcode specific data (1-4 bytes)
            - the opcode specific data
        AddChannelRequest and DropChannel have this structure.
        """

        if self._read_position + size_of_size > len(self._data):
            raise InvalidMuxControlBlockException(
                'No size field for opcode %d' % opcode)

        pos = self._read_position
        size = 0
        if size_of_size == 1:
            size = ord(self._data[pos])
            pos += 1
        elif size_of_size == 2:
            size = struct.unpack('!H', self._data[pos:pos+2])[0]
            pos += 2
        elif size_of_size == 3:
            size = ord(self._data[pos]) << 16
            size += struct.unpack('!H', self._data[pos+1:pos+3])[0]
            pos += 3
        elif size_of_size == 4:
            size = struct.unpack('!L', self._data[pos:pos+4])[0]
            pos += 4
        else:
            raise InvalidMuxControlBlockException(
                'Invalid size of the size field for opcode %d' % opcode)

        if pos + size > len(self._data):
            raise InvalidMuxControlBlockException(
                'No data field for opcode %d (%d + %d > %d)' %
                (opcode, pos, size, len(self._data)))

        specific_data = self._data[pos:pos+size]
        self._read_position = pos + size
        return specific_data

    def _read_add_channel_request(self, first_byte, control_block):
        reserved = (first_byte >> 4) & 0x1
        encoding = (first_byte >> 2) & 0x3
        size_of_handshake_size = (first_byte & 0x3) + 1

        control_block.channel_id = self.read_channel_id()
        encoded_handshake = self._read_opcode_specific_data(
                                _MUX_OPCODE_ADD_CHANNEL_REQUEST,
                                size_of_handshake_size)
        control_block.encoding = encoding
        control_block.encoded_handshake = encoded_handshake
        return control_block

    def _read_flow_control(self, first_byte, control_block):
        # TODO(bashi): Implement
        raise MuxNotImplementedException('FlowControl is not implemented')

    def _read_drop_channel(self, first_byte, control_block):
        mux_error = (first_byte >> 4) & 0x1
        reserved = (first_byte >> 2) & 0x3
        size_of_reason_size = (first_byte & 0x3) + 1

        control_block.channel_id = self.read_channel_id()
        reason = self._read_opcode_specific_data(
                     _MUX_OPCODE_ADD_CHANNEL_RESPONSE,
                     size_of_reason_size)
        if mux_error and len(reason) > 0:
            raise InvalidMuxControlBlockException(
                'Reason must be empty when F bit is set')
        control_block.mux_error = mux_error
        control_block.reason = reason
        return control_block

    def _read_new_channel_slot(self, first_byte, control_block):
        # TODO(bashi): Implement
        raise MuxNotImplementedException('NewChannelSlot is not implemented')

    def read_control_blocks(self):
        """Reads control block(s).

        Raises:
           InvalidMuxControlBlock: when the payload contains invalid control
               block(s).
           StopIteration: when no control blocks left.
        """

        while self._read_position < len(self._data):
            if self._read_position >= len(self._data):
                raise InvalidMuxControlBlockException(
                    'No control opcode found')
            first_byte = ord(self._data[self._read_position])
            self._read_position += 1
            opcode = (first_byte >> 5) & 0x7
            control_block = _ControlBlock(opcode=opcode)
            if opcode == _MUX_OPCODE_ADD_CHANNEL_REQUEST:
                yield self._read_add_channel_request(first_byte, control_block)
            elif opcode == _MUX_OPCODE_FLOW_CONTROL:
                yield self._read_flow_control(first_byte, control_block)
            elif opcode == _MUX_OPCODE_DROP_CHANNEL:
                yield self._read_drop_channel(first_byte, control_block)
            elif opcode == _MUX_OPCODE_NEW_CHANNEL_SLOT:
                yield self._read_new_channel_slot(first_byte, control_block)
            else:
                raise InvalidMuxControlBlockException(
                    'Invalid opcode %d' % opcode)
        assert self._read_position == len(self._data)
        raise StopIteration

    def remaining_data(self):
        """Returns remaining data."""

        return self._data[self._read_position:]


class _LogicalRequest(object):
    """Mimics mod_python request."""

    def __init__(self, channel_id, command, path, headers, connection):
        """Constructs an instance.

        Args:
            channel_id: the channel id of the logical channel.
            command: HTTP request command.
            path: HTTP request path.
            headers: HTTP headers.
            connection: _LogicalConnection instance.
        """

        self.channel_id = channel_id
        self.method = command
        self.uri = path
        self.headers_in = headers
        self.connection = connection
        self.server_terminated = False
        self.client_terminated = False

    def is_https(self):
        """Mimics request.is_https(). Returns False because this method is
        used only by old protocols (hixie and hybi00).
        """

        return False


class _LogicalConnection(object):
    """Mimics mod_python mp_conn."""

    # For details, see the comment of set_read_state().
    STATE_ACTIVE = 1
    STATE_GRACEFULLY_CLOSED = 2
    STATE_TERMINATED = 3

    def __init__(self, mux_handler, channel_id):
        """Constructs an instance.

        Args:
            mux_handler: _MuxHandler instance.
            channel_id: channel id of this connection.
        """

        self._mux_handler = mux_handler
        self._channel_id = channel_id
        self._incoming_data = ''
        self._write_condition = threading.Condition()
        self._waiting_write_completion = False
        self._read_condition = threading.Condition()
        self._read_state = self.STATE_ACTIVE

    def get_local_addr(self):
        """Getter to mimic mp_conn.local_addr."""

        return self._mux_handler.physical_connection.get_local_addr()
    local_addr = property(get_local_addr)

    def get_remote_addr(self):
        """Getter to mimic mp_conn.remote_addr."""

        return self._mux_handler.physical_connection.get_remote_addr()
    remote_addr = property(get_remote_addr)

    def get_memorized_lines(self):
        """Gets memorized lines. Not supported."""

        raise MuxUnexpectedException('_LogicalConnection does not support '
                                     'get_memorized_lines')

    def write(self, data):
        """Writes data. mux_handler sends data asynchronously. The caller will
        be suspended until write done.

        Args:
            data: data to be written.

        Raises:
            MuxUnexpectedException: when called before finishing the previous
                write.
        """

        try:
            self._write_condition.acquire()
            if self._waiting_write_completion:
                raise MuxUnexpectedException(
                    'Logical connection %d is already waiting the completion '
                    'of write' % self._channel_id)

            self._waiting_write_completion = True
            self._mux_handler.send_data(self._channel_id, data)
            self._write_condition.wait()
        finally:
            self._write_condition.release()

    def write_control_data(self, data):
        """Writes data via the control channel. Don't wait finishing write
        because this method can be called by mux dispatcher.

        Args:
            data: data to be written.
        """

        self._mux_handler.send_control_data(data)

    def notify_write_done(self):
        """Called when sending data is completed."""

        try:
            self._write_condition.acquire()
            if not self._waiting_write_completion:
                raise MuxUnexpectedException(
                    'Invalid call of notify_write_done for logical connection'
                    ' %d' % self._channel_id)
            self._waiting_write_completion = False
            self._write_condition.notify()
        finally:
            self._write_condition.release()

    def append_frame_data(self, frame_data):
        """Appends incoming frame data. Called when mux_handler dispatches
        frame data to the corresponding application.

        Args:
            frame_data: incoming frame data.
        """

        self._read_condition.acquire()
        self._incoming_data += frame_data
        self._read_condition.notify()
        self._read_condition.release()

    def read(self, length):
        """Reads data. Blocks until enough data has arrived via physical
        connection.

        Args:
            length: length of data to be read.
        Raises:
            LogicalConnectionClosedException: when closing handshake for this
                logical channel has been received.
            ConnectionTerminatedException: when the physical connection has
                closed, or an error is caused on the reader thread.
        """

        self._read_condition.acquire()
        while (self._read_state == self.STATE_ACTIVE and
               len(self._incoming_data) < length):
            self._read_condition.wait()

        try:
            if self._read_state == self.STATE_GRACEFULLY_CLOSED:
                raise LogicalConnectionClosedException(
                    'Logical channel %d has closed.' % self._channel_id)
            elif self._read_state == self.STATE_TERMINATED:
                raise ConnectionTerminatedException(
                    'Receiving %d byte failed. Logical channel (%d) closed' %
                    (length, self._channel_id))

            value = self._incoming_data[:length]
            self._incoming_data = self._incoming_data[length:]
        finally:
            self._read_condition.release()

        return value

    def set_read_state(self, new_state):
        """Sets the state of this connection. Called when an event for this
        connection has occurred.

        Args:
            new_state: state to be set. new_state must be one of followings:
            - STATE_GRACEFULLY_CLOSED: when closing handshake for this
                connection has been received.
            - STATE_TERMINATED: when the physical connection has closed or
                DropChannel of this connection has received.
        """

        self._read_condition.acquire()
        self._read_state = new_state
        self._read_condition.notify()
        self._read_condition.release()


class _LogicalStream(Stream):
    """Mimics the Stream class. This class interprets multiplexed WebSocket
    frames.
    """

    def __init__(self, request):
        """Constructs an instance.

        Args:
            request: _LogicalRequest instance.
        """

        # TODO(bashi): Support frame filters.
        stream_options = StreamOptions()
        # Physical stream is responsible for masking.
        stream_options.unmask_receive = False
        Stream.__init__(self, request, stream_options)

    def _create_inner_frame(self, opcode, payload, end=True):
        # TODO(bashi): Support extensions that use reserved bits.
        bits = (end << 7) | opcode
        if opcode == common.OPCODE_TEXT:
            payload = payload.encode('utf-8')

        return (_encode_channel_id(self._request.channel_id) +
                chr(bits) + payload)

    def send_message(self, message, end=True, binary=False):
        """Override Stream.send_message."""

        if self._request.server_terminated:
            raise BadOperationException(
                'Requested send_message after sending out a closing handshake')

        if binary and isinstance(message, unicode):
            raise BadOperationException(
                'Message for binary frame must be instance of str')

        try:
            if binary:
                opcode = common.OPCODE_BINARY
            else:
                opcode = common.OPCODE_TEXT

            payload = self._create_inner_frame(opcode, message, end)
            frame_data = self._writer.build(payload, end=True, binary=True)
            self._request.connection.write(frame_data)
        except ValueError, e:
            raise BadOperationException(e)

    def receive_message(self):
        """Overrides Stream.receive_message."""

        # Just call Stream.receive_message(), but catch
        # LogicalConnectionClosedException, which is raised when the logical
        # connection has closed gracefully.
        try:
            return Stream.receive_message(self)
        except LogicalConnectionClosedException, e:
            self._logger.debug('%s', e)
            return None

    def _send_closing_handshake(self, code, reason):
        """Overrides Stream._send_closing_handshake."""

        body = create_closing_handshake_body(code, reason)
        data = self._create_inner_frame(common.OPCODE_CLOSE, body, end=True)
        frame_data = create_binary_frame(data, mask=False)

        self._request.server_terminated = True
        self._logger.debug('Sending closing handshake for %d: %r' %
                           (self._request.channel_id, frame_data))
        self._request.connection.write(frame_data)

    def send_ping(self, body=''):
        """Overrides Stream.send_ping"""

        data = self._create_inner_frame(common.OPCODE_PING, body,
                                        end=True)
        frame_data = create_binary_frame(data, mask=False)

        self._logger.debug('Sending ping on logical channel %d: %r' %
                           (self._request.channel_id, frame_data))
        self._request.connection.write(frame_data)

        self._ping_queue.append(body)

    def _send_pong(self, body):
        """Overrides Stream._send_pong"""

        data = self._create_inner_frame(common.OPCODE_PONG, body,
                                        end=True)
        frame_data = create_binary_frame(data, mask=False)

        self._logger.debug('Sending pong on logical channel %d: %r' %
                           (self._request.channel_id, frame_data))
        self._request.connection.write(frame_data)

    def close_connection(self, code=common.STATUS_NORMAL_CLOSURE, reason=''):
        """Overrides Stream.close_connection."""

        # TODO(bashi): Implement
        self._logger.debug('Closing logical connection %d' %
                           self._request.channel_id)
        self._request.server_terminated = True

    def _drain_received_data(self):
        """Overrides Stream._drain_received_data. Nothing need to be done for
        logical channel.
        """

        pass


class _OutgoingData(object):
    """A structure that holds data to be sent via physical connection and
    origin of the data.
    """

    def __init__(self, channel_id, data):
        self.channel_id = channel_id
        self.data = data


class _PhysicalConnectionWriter(threading.Thread):
    """A thread that is responsible for writing data to physical connection.

    TODO(bashi): Make sure there is no thread-safety problem when the reader
    thread reads data from the same socket at a time.
    """

    def __init__(self, mux_handler):
        """Constructs an instance.

        Args:
            mux_handler: _MuxHandler instance.
        """

        threading.Thread.__init__(self)
        self._logger = util.get_class_logger(self)
        self._mux_handler = mux_handler
        self.setDaemon(True)
        self._stop_requested = False
        self._deque = collections.deque()
        self._deque_condition = threading.Condition()

    def put_outgoing_data(self, data):
        """Puts outgoing data.

        Args:
            data: _OutgoingData instance.

        Raises:
            BadOperationException: when the thread has been requested to
                terminate.
        """

        try:
            self._deque_condition.acquire()
            if self._stop_requested:
                raise BadOperationException('Cannot write data anymore')

            self._deque.append(data)
            self._deque_condition.notify()
        finally:
            self._deque_condition.release()

    def _write_data(self, outgoing_data):
        try:
            self._mux_handler.physical_connection.write(outgoing_data.data)
        except Exception, e:
            util.prepend_message_to_exception(
                'Failed to send message to %r: ' %
                (self._mux_handler.physical_connection.remote_addr,), e)
            raise

        # TODO(bashi): It would be better to block the thread that sends
        # control data as well.
        if outgoing_data.channel_id != _CONTROL_CHANNEL_ID:
            self._mux_handler.notify_write_done(outgoing_data.channel_id)

    def run(self):
        self._deque_condition.acquire()
        while not self._stop_requested:
            if len(self._deque) == 0:
                self._deque_condition.wait()
                continue

            outgoing_data = self._deque.popleft()
            self._deque_condition.release()
            self._write_data(outgoing_data)
            self._deque_condition.acquire()

        # Flush deque
        try:
            while len(self._deque) > 0:
                outgoing_data = self._deque.popleft()
                self._write_data(outgoing_data)
        finally:
            self._deque_condition.release()

    def stop(self):
        """Stops the writer thread."""

        self._deque_condition.acquire()
        self._stop_requested = True
        self._deque_condition.notify()
        self._deque_condition.release()


class _PhysicalConnectionReader(threading.Thread):
    """A thread that is responsible for reading data from physical connection.
    """

    def __init__(self, mux_handler):
        """Constructs an instance.

        Args:
            mux_handler: _MuxHandler instance.
        """

        threading.Thread.__init__(self)
        self._logger = util.get_class_logger(self)
        self._mux_handler = mux_handler
        self.setDaemon(True)

    def run(self):
        while True:
            try:
                physical_stream = self._mux_handler.physical_stream
                frame = physical_stream._receive_frame_as_frame_object()
            except ConnectionTerminatedException, e:
                self._logger.debug('%s', e)
                break

            try:
                self._mux_handler.dispatch_frame(frame)
            except Exception, e:
                self._logger.debug(traceback.format_exc())
                break

        self._mux_handler.notify_reader_done()


class _Worker(threading.Thread):
    """A thread that is responsible for running the corresponding application
    handler.
    """

    def __init__(self, mux_handler, request):
        """Constructs an instance.

        Args:
            mux_handler: _MuxHandler instance.
            request: _LogicalRequest instance.
        """

        threading.Thread.__init__(self)
        self._logger = util.get_class_logger(self)
        self._mux_handler = mux_handler
        self._request = request
        self.setDaemon(True)

    def run(self):
        self._logger.debug('Logical channel worker started. (id=%d)' %
                           self._request.channel_id)
        try:
            # Non-critical exceptions will be handled by dispatcher.
            self._mux_handler.dispatcher.transfer_data(self._request)
        finally:
            self._mux_handler.notify_worker_done(self._request.channel_id)


class _MuxHandshaker(hybi.Handshaker):
    """Opening handshake processor for multiplexing."""

    def __init__(self, request, dispatcher):
        """Constructs an instance.
        Args:
            request: _LogicalRequest instance.
            dispatcher: Dispatcher instance (dispatch.Dispatcher).
        """

        hybi.Handshaker.__init__(self, request, dispatcher)

    def _create_stream(self, stream_options):
        """Override hybi.Handshaker._create_stream."""

        self._logger.debug('Creating logical stream for %d' %
                           self._request.channel_id)
        return _LogicalStream(self._request)

    def _send_handshake(self, accept):
        """Override hybi.Handshaker._send_handshake."""

        # Don't send handshake response for the default channel
        if self._request.channel_id == _DEFAULT_CHANNEL_ID:
            return

        handshake_response = self._create_handshake_response(accept)
        frame_data = _create_add_channel_response(
                         self._request.channel_id,
                         handshake_response)
        self._logger.debug('Sending handshake response for %d: %r' %
                           (self._request.channel_id, frame_data))
        self._request.connection.write_control_data(frame_data)


class _LogicalChannelData(object):
    """A structure that holds the logical request and the worker thread
    which are associated with a corresponding logical channel.
    """

    def __init__(self, request, worker):
        self.request = request
        self.worker = worker


class _MuxHandler(object):
    """Multiplexing handler. When a handler starts, it launches three
    threads; the reader thread, the writer thread, and a worker thread.

    The reader thread reads data from the physical stream, i.e., the
    ws_stream object of the underlying websocket connection. The reader
    thread interprets multiplexed frames and dispatches them to logical
    channels. Methods of this class are mostly called by the reader thread.

    The writer thread sends multiplexed frames which are created by
    logical channels via the physical connection.

    The worker thread launched at the starting point handles the
    "Implicitly Opened Connection". If multiplexing handler receives
    an AddChannelRequest and accepts it, the handler will launch a new worker
    thread and dispatch the request to it.
    """

    def __init__(self, request, dispatcher):
        """Constructs an instance.

        Args:
            request: mod_python request of the physical connection.
            dispatcher: Dispatcher instance (dispatch.Dispatcher).
        """

        self.original_request = request
        self.dispatcher = dispatcher
        self.physical_connection = request.connection
        self.physical_stream = request.ws_stream

        self._logger = util.get_class_logger(self)
        self._logical_channels = {}
        self._logical_channels_condition = threading.Condition()
        self._worker_done_notify_received = False
        self._reader = None
        self._writer = None

    def start(self):
        """Starts the handler.

        Raises:
            MuxUnexpectedException: when the handler already started, or when
                opening handshake of the default channel fails.
        """

        if self._reader or self._writer:
            raise MuxUnexpectedException('MuxHandler already started')

        self._reader = _PhysicalConnectionReader(self)
        self._writer = _PhysicalConnectionWriter(self)
        self._reader.start()
        self._writer.start()

        # Create "Implicitly Opened Connection".
        logical_connection = _LogicalConnection(self, _DEFAULT_CHANNEL_ID)
        headers_in = copy.copy(self.original_request.headers_in)
        # TODO(bashi): Support extensions
        headers_in['Sec-WebSocket-Extensions'] = ''
        logical_request = _LogicalRequest(_DEFAULT_CHANNEL_ID,
                                          self.original_request.method,
                                          self.original_request.uri,
                                          headers_in,
                                          logical_connection)
        if not self._do_handshake_for_logical_request(logical_request):
            raise MuxUnexpectedException(
                'Failed handshake on the default channel id')
        self._add_logical_channel(logical_request)

    def wait_until_done(self, timeout=None):
        """Waits until all workers are done. Returns False when timeout has
        occurred. Returns True on success.

        Args:
            timeout: timeout in sec.
        """

        self._logical_channels_condition.acquire()
        try:
            while len(self._logical_channels) > 0:
                self._logger.debug('Waiting workers(%d)...' %
                                   len(self._logical_channels))
                self._worker_done_notify_received = False
                self._logical_channels_condition.wait(timeout)
                if not self._worker_done_notify_received:
                    self._logger.debug('Waiting worker(s) timed out')
                    return False

        finally:
            self._logical_channels_condition.release()

        # Flush pending outgoing data
        self._writer.stop()
        self._writer.join()

        return True

    def notify_write_done(self, channel_id):
        """Called by the writer thread when a write operation has done.

        Args:
            channel_id: objective channel id.
        """

        try:
            self._logical_channels_condition.acquire()
            if channel_id in self._logical_channels:
                channel_data = self._logical_channels[channel_id]
                channel_data.request.connection.notify_write_done()
            else:
                self._logger.debug('Seems that logical channel for %d has gone'
                                   % channel_id)
        finally:
            self._logical_channels_condition.release()

    def send_control_data(self, data):
        """Sends data via the control channel.

        Args:
            data: data to be sent.
        """

        self._writer.put_outgoing_data(_OutgoingData(
                channel_id=_CONTROL_CHANNEL_ID, data=data))

    def send_data(self, channel_id, data):
        """Sends data via given logical channel. This method is called by
        worker threads.

        Args:
            channel_id: objective channel id.
            data: data to be sent.
        """

        self._writer.put_outgoing_data(_OutgoingData(
                channel_id=channel_id, data=data))

    def _send_drop_channel(self, channel_id, reason='', mux_error=False):
        frame_data = _create_drop_channel(channel_id, reason, mux_error)
        self._logger.debug(
            'Sending drop channel for channel id %d' % channel_id)
        self.send_control_data(frame_data)

    def _send_error_add_channel_response(self, channel_id, status=None):
        if status is None:
            status = common.HTTP_STATUS_BAD_REQUEST

        if status in _HTTP_BAD_RESPONSE_MESSAGES:
            message = _HTTP_BAD_RESPONSE_MESSAGES[status]
        else:
            self._logger.debug('Response message for %d is not found' % status)
            message = '???'

        response = 'HTTP/1.1 %d %s\r\n\r\n' % (status, message)
        frame_data = _create_add_channel_response(channel_id,
                                                  encoded_handshake=response,
                                                  encoding=0, rejected=True)
        self.send_control_data(frame_data)

    def _create_logical_request(self, block):
        if block.channel_id == _CONTROL_CHANNEL_ID:
            raise MuxUnexpectedException(
                'Received the control channel id (0) as objective channel '
                'id for AddChannel')

        if block.encoding != 0:
            raise MuxNotImplementedException(
                'delta-encoding not supported yet')
        connection = _LogicalConnection(self, block.channel_id)
        command, path, version, headers = _parse_request_text(
                                              block.encoded_handshake)
        request = _LogicalRequest(block.channel_id, command, path,
                                  headers, connection)

        return request

    def _do_handshake_for_logical_request(self, request):
        handshaker = _MuxHandshaker(request, self.dispatcher)
        try:
            handshaker.do_handshake()
        except handshake.VersionException, e:
            self._logger.info('%s', e)
            self._send_error_add_channel_response(
                block.channel_id, status=common.HTTP_STATUS_BAD_REQUEST)
            return False
        except handshake.HandshakeException, e:
            self._logger.info('%s', e)
            self._send_error_add_channel_response(request.channel_id,
                                                  status=e.status)
            return False
        except handshake.AbortedByUserException, e:
            self._logger.info('%s', e)
            self._send_error_add_channel_response(request.channel_id)
            return False

        return True

    def _add_logical_channel(self, logical_request):
        self._logical_channels_condition.acquire()
        if logical_request.channel_id in self._logical_channels:
            self._logical_channels_condition.release()
            raise MuxUnexpectedException(
                'Channel id %d already exists' % logical_request.channel_id)
        worker = _Worker(self, logical_request)
        channel_data = _LogicalChannelData(logical_request, worker)
        self._logical_channels[logical_request.channel_id] = channel_data
        worker.start()
        self._logical_channels_condition.release()

    def _process_add_channel_request(self, block):
        try:
            logical_request = self._create_logical_request(block)
        except ValueError, e:
            self._logger.debug('Failed to create logical request: %r' % e)
            self._send_error_add_channel_response(
                block.channel_id, status=common.HTTP_STATUS_BAD_REQUEST)
            return
        if self._do_handshake_for_logical_request(logical_request):
            self._add_logical_channel(logical_request)

    def _process_flow_control(self, block):
        # TODO(bashi): Implement
        raise MuxNotImplementedException('FlowControl is not implemented')

    def _process_drop_channel(self, block):
        self._logger.debug('DropChannel received for %d: reason=%r' %
                           (block.channel_id, block.reason))
        try:
            self._logical_channels_condition.acquire()
            if not block.channel_id in self._logical_channels:
                return
            channel_data = self._logical_channels[block.channel_id]
            if not block.mux_error:
                channel_data.request.connection.set_read_state(
                    _LogicalConnection.STATE_TERMINATED)
            else:
                # TODO(bashi): What should we do?
                channel_data.request.connection.set_read_state(
                    _LogicalConnection.STATE_TERMINATED)
        finally:
            self._logical_channels_condition.release()

    def _process_new_channel_slot(self, block):
        # TODO(bashi): Implement
        raise MuxNotImplementedException(
            'NewChannelSlot is not implemented')

    def _process_control_blocks(self, parser):
        for control_block in parser.read_control_blocks():
            opcode = control_block.opcode
            self._logger.debug('control block received, opcode: %d' % opcode)
            if opcode == _MUX_OPCODE_ADD_CHANNEL_REQUEST:
                self._process_add_channel_request(control_block)
            elif opcode == _MUX_OPCODE_FLOW_CONTROL:
                self._process_flow_control(control_block)
            elif opcode == _MUX_OPCODE_DROP_CHANNEL:
                self._process_drop_channel(control_block)
            elif opcode == _MUX_OPCODE_NEW_CHANNEL_SLOT:
                self._process_new_channel_slot(control_block)
            else:
                raise InvalidMuxControlBlockException(
                    'Invalid opcode')

    def _dispatch_frame_to_logical_channel(self, channel_id, frame_data):
        try:
            self._logical_channels_condition.acquire()
            if not channel_id in self._logical_channels:
                raise MuxUnexpectedException(
                    'Channel id %d not found' % channel_id)
            channel_data = self._logical_channels[channel_id]
            channel_data.request.connection.append_frame_data(frame_data)
        finally:
            self._logical_channels_condition.release()

    def dispatch_frame(self, frame):
        """Dispatches frame. The reader thread calls this method.

        Args:
            frame: a multiplexed frame to be dispatched.
        Raises:
            InvalidMuxFrame: if the frame is invalid.
        """

        parser = _MuxFramePayloadParser(frame.payload)
        channel_id = parser.read_channel_id()
        if channel_id == _CONTROL_CHANNEL_ID:
            self._process_control_blocks(parser)
        else:
            self._logger.debug('Received a frame. channel id=%d' % channel_id)
            frame_data = parser.read_inner_frame()
            self._dispatch_frame_to_logical_channel(channel_id, frame_data)

    def notify_worker_done(self, channel_id):
        """Called when a worker has finished.

        Args:
            channel_id: channel id corresponded with the worker.
        """

        self._logger.debug('Worker for channel id %d terminated' % channel_id)
        try:
            self._logical_channels_condition.acquire()
            if not channel_id in self._logical_channels:
                raise MuxUnexpectedException(
                    'Channel id %d not found' % channel_id)
            channel_data = self._logical_channels.pop(channel_id)
        finally:
            self._worker_done_notify_received = True
            self._logical_channels_condition.notify()
            self._logical_channels_condition.release()

        if not channel_data.request.server_terminated:
            self._send_drop_channel(channel_id)

    def notify_reader_done(self):
        """This method is called by the reader thread when the reader has
        finished.
        """

        # Terminate all logical connections
        self._logger.debug('termiating all logical connections...')
        self._logical_channels_condition.acquire()
        for channel_data in self._logical_channels.values():
            try:
                channel_data.request.connection.set_read_state(
                    _LogicalConnection.STATE_TERMINATED)
            except Exception:
                pass
        self._logical_channels_condition.release()


# vi:sts=4 sw=4 et
