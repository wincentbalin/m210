# -*- coding: utf-8 -*-
# python-m210
# Copyright © 2010 Tuomas Räsänen (tuos) <tuos@codegrove.org>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Python API to Pegasus Notetaker devices.

Currently only Pegasus Mobile Notetaker `M210`_ is supported.

.. _M210: http://www.pegatech.com/?CategoryID=207&ArticleID=268

"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import collections
import errno
import os
import select
import struct

import m210.hidraw

class OrderedSet(collections.MutableSet):

    KEY, PREV, NEXT = range(3)

    def __init__(self, iterable=None):
        self.end = end = [] 
        end += [None, end, end]         # sentinel node for doubly linked list
        self.map = {}                   # key --> [key, prev, next]
        if iterable is not None:
            self |= iterable

    def __getitem__(self, i):
        return tuple(self)[i]

    def __len__(self):
        return len(self.map)

    def __contains__(self, key):
        return key in self.map

    def add(self, key):
        if key not in self.map:
            end = self.end
            curr = end[OrderedSet.PREV]
            curr[OrderedSet.NEXT] = end[OrderedSet.PREV] = self.map[key] = [key, curr, end]

    def discard(self, key):
        if key in self.map:        
            key, prev, next = self.map.pop(key)
            prev[OrderedSet.NEXT] = next
            next[OrderedSet.PREV] = prev

    def __iter__(self):
        end = self.end
        curr = end[OrderedSet.NEXT]
        while curr is not end:
            yield curr[OrderedSet.KEY]
            curr = curr[OrderedSet.NEXT]

    def __reversed__(self):
        end = self.end
        curr = end[OrderedSet.PREV]
        while curr is not end:
            yield curr[OrderedSet.KEY]
            curr = curr[OrderedSet.PREV]

    def pop(self, last=True):
        if not self:
            raise KeyError('set is empty')
        key = next(reversed(self)) if last else next(iter(self))
        self.discard(key)
        return key

    def __repr__(self):
        if not self:
            return '%s()' % (self.__class__.__name__,)
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def __eq__(self, other):
        if isinstance(other, OrderedSet):
            return len(self) == len(other) and list(self) == list(other)
        return set(self) == set(other)

    def __del__(self):
        self.clear()                    # remove circular references

__all__ =  [
    "CommunicationError",
    "TimeoutError",
    "M210",
    ]

class CommunicationError(Exception):
    """Raised when an unexpected message is received."""
    pass

class TimeoutError(Exception):
    """Raised when communication timeouts."""
    pass

class TabletSizeError(Exception):
    pass

class TabletOrientationError(Exception):
    pass

class ModeError(Exception):
    pass

class M210(object):
    """
    M210 exposes two interfaces via USB-connection. By default, udev
    creates two hidraw devices to represent these interfaces when the
    device is plugged in. Paths to these devices must be passed to
    initialize a M210-connection. Interface 1 is used for reading and
    writing, interface 2 only for reading.

    Usage example::

      >>> import pegatech
      >>> dev = pegatech.M210(("/dev/hidraw1", "/dev/hidraw2"))
      >>> dev.get_info()
      {'used_memory': 1364, 'firmware_version': 337, 'analog_version': 265, 'pad_version': 32028, 'mode': 'TABLET'}
      >>> download_destination = open("m210notes", "wb")
      >>> dev.download_notes_to(download_destination)
      1364
      >>> download_destination.tell()
      1364
      >>> dev.delete_notes()
      >>> dev.get_info()
      {'used_memory': 0, 'firmware_version': 337, 'analog_version': 265, 'pad_version': 32028, 'mode': 'TABLET'}
    
    """

    _MODE_BYTE_TO_STR_MAP = {'\x01': 'MOUSE', '\x02': 'TABLET'}
    _MODE_STR_TO_BYTE_MAP = {'MOUSE': '\x01', 'TABLET': '\x02'}
    _MODE_BYTE_TO_INDICATOR_MAP = {'\x01': '\x02', '\x02': '\x01'}

    _ORIENTATION_STR_TO_BYTE_MAP = {'NW': '\x01', 'N': '\x00', 'NE': '\x02'}

    _PACKET_PAYLOAD_SIZE = 62

    def __init__(self, hidraw_filepaths, read_timeout=1.0):
        self.read_timeout = read_timeout
        self._fds = []
        for filepath, mode in zip(hidraw_filepaths, (os.O_RDWR, os.O_RDONLY)):
            fd = os.open(filepath, mode)
            devinfo = m210.hidraw.get_devinfo(fd)
            if devinfo != {'product': 257, 'vendor': 3616, 'bustype': 3}:
                raise ValueError('%s is not a M210 hidraw device.' % filepath)
            self._fds.append(fd)

    def _read(self, iface_n):
        fd = self._fds[iface_n]

        rlist, _, _ = select.select([fd], [], [], self.read_timeout)
        if not fd in rlist:
            raise TimeoutError("Reading timeouted.")

        response_size = (64, 9)[iface_n]

        while True:
            response = os.read(fd, response_size)

            if iface_n == 0:
                if response[:2] == '\x80\xb5':
                    continue # Ignore mode button events, at least for now.
                if response[0] in '\x40\x41\x42':
                    if response[1:6] == '\x00\x00\x00\x00\x00':
                        continue # Ignore pen-up packets.
                    if response[1] in '\x01\x02\x03\x08':
                        continue # Ignore pen-data packets.
            break

        return response

    def _write(self, request):
        request_header = struct.pack('BBB', 0x00, 0x02, len(request))
        os.write(self._fds[0], request_header + request)

    def _wait_ready(self):
        while True:
            self._write('\x95')
            try:
                response = self._read(0)
            except TimeoutError:
                continue
            break

        if (response[:3] != '\x80\xa9\x28'
            or response[9] != '\x0e'):
            raise CommunicationError('Unexpected response to info request: %s'
                                     % response)

        mode_str = M210._MODE_BYTE_TO_STR_MAP[response[10]]

        firmware_ver, analog_ver, pad_ver = struct.unpack('>HHH', response[3:9])

        return {'firmware_version': firmware_ver,
                'analog_version': analog_ver,
                'pad_version': pad_ver,
                'mode': mode_str}

    def _accept_upload(self):
        self._write('\xb6')

    def _reject_upload(self):
        self._write('\xb7')

    def _begin_upload(self):
        """Return packet count."""

        self._write('\xb5')
        try:
            try:
                response = self._read(0)
            except TimeoutError:
                # M210 with zero notes stored in it does not send any response.
                return 0
            if (not response.startswith('\xaa\xaa\xaa\xaa\xaa')
                or response[7:9] != '\x55\x55'):
                raise CommunicationError("Unrecognized upload response: %s"
                                         % response[:9])
            return struct.unpack('>H', response[5:7])[0]
        except Exception, e:
            # If just anything goes wrong, try to leave the device in
            # a decent state by sending a reject request. Then just
            # raise the original exception.
            try:
                self._reject_upload()
            except:
                pass
            raise e

    def set_tablet_settings(self, size, orientation):
        if size not in range(1, 10):
            raise TabletSizeError(size)

        try:
            orient_byte = M210._ORIENTATION_STR_TO_BYTE_MAP[orientation]
        except KeyError:
            raise TabletOrientationError(orientation)

        # Size byte in M210 is counter-intuitive: 0x01 means the largest,
        # 0x09 the smallest. However, in our API, sizes are handled
        # intuitively and therefore needs to be changed here.
        size_byte = chr(abs(size - 9) + 1)

        # Their protocol-specificaion (docs/xy.html) says, that size
        # can in the range of [0,9]. However, 0 does not seem to
        # change the size.

        self._wait_ready()
        self._write(''.join(('\x80\xb6', size_byte, orient_byte)))

    def set_mode(self, new_mode):
        """Set the operation mode of the device. 
        Value of `new_mode` should be 'TABLET' or 'MOUSE', ModeError
        is raised otherwise.
        """

        try:
            mode_byte = M210._MODE_STR_TO_BYTE_MAP[new_mode]
        except KeyError:
            raise ModeError(new_mode)

        mode_indicator = M210._MODE_BYTE_TO_INDICATOR_MAP[mode_byte]

        self._wait_ready()
        self._write(''.join(('\x80\xb5', mode_indicator, mode_byte)))

    def get_info(self):
        """Return a dict containing information about versions, current mode
        and the size of stored notes in bytes.
        """

        info = self._wait_ready()
        packet_count = self._begin_upload()
        self._reject_upload()
        info['used_memory'] = packet_count * M210._PACKET_PAYLOAD_SIZE
        return info

    def delete_notes(self):
        """Delete all notes stored in the device."""

        self._wait_ready()
        self._write('\xb0')

    def _receive_packet(self):
        response = self._read(0)
        packet_number = struct.unpack('>H', response[:2])[0]
        return packet_number, response[2:]

    def packet_payload_iter(self):
        # Wait until the device is ready to upload packages to
        # us. This is needed because previous public API call might
        # have failed and the device hasn't had enough time to recover
        # from that incident yet.
        self._wait_ready()

        packet_count = self._begin_upload()
        if packet_count == 0:
            # The device does not have any notes, inform that it's ok
            # and let it rest.
            self._reject_upload()
            return

        self._accept_upload()
        lost_packet_numbers = OrderedSet()

        # For some odd reason, packet numbering starts from 1 in
        # M210's memory.
        for expected_number in range(1, packet_count + 1):

            packet_number, packet_payload = self._receive_packet()

            if packet_number != expected_number:
                lost_packet_numbers.add(expected_number)

            if not lost_packet_numbers:
                # It's safe to yield only when all expected packets so
                # far have been received. The behavior is changed as
                # soon as the first package is lost. This ensures the
                # correct order of data.
                yield packet_payload

        # Request resend of lost packets.
        while lost_packet_numbers:
            lost_packet_number = lost_packet_numbers[0]
            self._write('\xb7' + struct.pack('>H', lost_packet_number))
            packet_number, packet_payload = self._receive_packet()
            if packet_number == lost_packet_number:
                lost_packet_numbers.remove(lost_packet_number)
                yield packet_payload

        # Thank the device for cooperation and let it rest.
        self._accept_upload()

    def download_notes_to(self, destination_file):
        """Download notes to an open `destination_file` in one pass.

        Return the total size (bytes) of downloaded notes.
        """

        count = 0
        for packet_payload in self.packet_payload_iter():
            destination_file.write(packet_payload)
            count += 1
        return count * M210._PACKET_PAYLOAD_SIZE

_NOTE_STATE_MAP = {
    0x9f: "empty",
    0x5f: "unfinished",
    0x3f: "finished_by_user",
    0x1f: "finished_by_software",
    }

class FileFormatError(Exception):
    pass

class Note(object):

    def __init__(self, number):
        self.paths = []
        self.number = number

    def as_svg(self):
        svg_polylines = []
        for path in self.paths:
            points = " ".join(["%d,%d" % (x, y) for x, y in path])
            svg_polylines.append('<polyline points="%s" stroke-width="30" stroke="black" fill="none"/>' % points)
        return """<?xml version="1.0"?>
    <!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
    <svg width="210mm" height="297mm" viewBox="-7000 500 14000 20000" xmlns="http://www.w3.org/2000/svg" version="1.1">
        %s
    </svg>
    """ % "\n    ".join(svg_polylines)

def note_iter_from_file(notes_file):

    class counting_read(object):

        def __init__(self, f):
            self.f = f
            self.fpos = 0

        def __call__(self, byte_count, *args, **kwargs):
            bytes = self.f.read(byte_count)
            self.fpos += len(bytes)
            return bytes

    notes_file_read = counting_read(notes_file)

    while True:

        # BEGIN HEADER.
        next_header_bytes = notes_file_read(3)
        if not next_header_bytes or next_header_bytes[0] == '\x00':
            break
        
        next_header_pos = struct.unpack("<I", next_header_bytes + "\x00")[0]

        state, note_num, note_max_num = struct.unpack("BBB", notes_file_read(3))
        if note_num > note_max_num:
            raise FileFormatError("Note number is bigger than the total number of notes.")
        notes_file_read(8) # Reserved for some unknown usage, perhaps timestamp?
        # END HEADER.

        # BEGIN DATA.
        note = Note(note_num)
        path = []
        while notes_file_read.fpos < next_header_pos:
            data = notes_file_read(4)
            if data == "\x00\x00\x00\x80":
                note.paths.append(path)
                path = []
            else:
                point = struct.unpack("<hh", data)
                path.append(point)
        if path:
            note.paths.append(path)
        # END DATA.

        yield note

def note_iter_from_data(data):

    class FileLikeWrapper(object):
        def __init__(self, data):
            self.data = data
            self._i = 0
            
        def read(self, byte_count):
            result = self.data[self._i:self._i + byte_count]
            self._i += byte_count
            return result

    return note_iter_from_file(FileLikeWrapper(data))

def export_notes_as_svgs(input_file, output_dirpath):
    try:
        os.mkdir(output_dirpath)
    except OSError, e:
        if e.errno != errno.EEXIST:
            raise e

    for note in note_iter_from_file(input_file):
        svgpath = os.path.join(output_dirpath, str(note.number) + ".svg")
        with open(svgpath, "w") as svgfile:
            svgfile.write(note.as_svg())
