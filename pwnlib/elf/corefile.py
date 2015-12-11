import collections
import ctypes
import elftools
from elftools.common.utils import roundup, struct_parse
from elftools.common.py3compat import bytes2str
from elftools.construct import CString


from ..context import context
from ..log import getLogger
from .datatypes import *
from .elf import ELF
from ..tubes.tube import tube

log = getLogger(__name__)

types = {
    'i386': elf_prstatus_i386,
    'amd64': elf_prstatus_amd64,
}

# Slightly modified copy of the pyelftools version of the same function,
# until they fix this issue:
# https://github.com/eliben/pyelftools/issues/93
def iter_notes(self):
    """ Iterates the list of notes in the segment.
    """
    offset = self['p_offset']
    end = self['p_offset'] + self['p_filesz']
    while offset < end:
        note = struct_parse(
            self._elfstructs.Elf_Nhdr,
            self.stream,
            stream_pos=offset)
        note['n_offset'] = offset
        offset += self._elfstructs.Elf_Nhdr.sizeof()
        self.stream.seek(offset)
        # n_namesz is 4-byte aligned.
        disk_namesz = roundup(note['n_namesz'], 2)
        note['n_name'] = bytes2str(
            CString('').parse(self.stream.read(disk_namesz)))
        offset += disk_namesz

        desc_data = bytes2str(self.stream.read(note['n_descsz']))
        note['n_desc'] = desc_data
        offset += roundup(note['n_descsz'], 2)
        note['n_size'] = offset - note['n_offset']
        yield note

class Mapping(object):
    def __init__(self, name, start, stop, flags):
        self.name=name
        self.start=start
        self.stop=stop
        self.size=stop-start
        self.flags=flags
    @property
    def permstr(self):
        flags = self.flags
        return ''.join(['r' if flags & 4 else '-',
                        'w' if flags & 2 else '-',
                        'x' if flags & 1 else '-',
                        'p'])
    def __str__(self):
        return '%x-%x %s %x %s' % (self.start,self.stop,self.permstr,self.size,self.name)

    def __repr__(self):
        return '%s(%r, %#x, %#x, %#x, %#x)' % (self.__class__.__name__,
                                               self.name,
                                               self.start,
                                               self.stop,
                                               self.size,
                                               self.flags)

    def __int__(self):
        return self.start

class Core(ELF):
    """Core(*a, **kw) -> Core

    Enhances the inforation available about a corefile (which is an extension
    of the ELF format) by permitting extraction of information about the mapped
    data segments, and register state.

    Registers can be accessed directly, e.g. via ``core_obj.eax``.

    Mappings can be iterated in order via ``core_obj.mappings``.
    """
    def __init__(self, *a, **kw):
        self.prstatus = None
        self.files    = {}
        self.mappings = []
        self.stack    = None
        self.env      = {}

        super(Core, self).__init__(*a, **kw)
        self.load_addr = 0
        self._address  = 0

        if not self.elftype == 'CORE':
            log.error("%s is not a valid corefile" % e.file.name)

        if not self.arch in ('i386','amd64'):
            log.error("%s does not use a supported corefile architecture" % e.file.name)

        prstatus_type = types[self.arch]

        with log.waitfor("Parsing corefile...") as w:
            for segment in self.segments:
                if not isinstance(segment, elftools.elf.segments.NoteSegment):
                    continue
                for note in iter_notes(segment):
                    # Try to find NT_PRSTATUS.  Note that pyelftools currently
                    # mis-identifies the enum name as 'NT_GNU_ABI_TAG'.
                    if note.n_descsz == ctypes.sizeof(prstatus_type) and \
                       note.n_type == 'NT_GNU_ABI_TAG':
                        self.NT_PRSTATUS = note
                        self.prstatus = prstatus_type.from_buffer_copy(note.n_desc)

                    # Try to find the list of mapped files
                    if note.n_type == constants.NT_FILE:
                        with context.local(bytes=self.bytes):
                            self._parse_nt_file(note)

                    # Try to find the auxiliary vector, which will tell us
                    # where the top of the stack is.
                    if note.n_type == constants.NT_AUXV:
                        with context.local(bytes=self.bytes):
                            self._parse_auxv(note)

            if self.stack and self.mappings:
                for mapping in self.mappings:
                    if mapping.stop == self.stack:
                        mapping.name = '[stack]'
                        self.stack   = mapping
                        with context.local(bytes=self.bytes):
                            self._parse_stack()

    def _parse_nt_file(self, note):
        t = tube()
        t.unrecv(note.n_desc)

        count = t.unpack()
        page_size = t.unpack()

        mappings = []
        addresses = {}

        for i in range(count):
            start = t.unpack()
            end = t.unpack()
            ofs = t.unpack()
            mapping = Mapping(None, start, end, 0)
            mappings.append(mapping)
            addresses[start] = mapping

        for i in range(count):
            filename = t.recvuntil('\x00', drop=True)
            mappings[i].name = filename

        for s in self.segments:
            if s.header.p_type != 'PT_LOAD':
                continue

            if s.header.p_vaddr in addresses:
                addresses[s.header.p_vaddr].flags = s.header.p_flags
            else:
                mapping = Mapping(None,
                                  s.header.p_vaddr,
                                  s.header.p_vaddr + s.header.p_memsz,
                                  s.header.p_flags)
                mappings.append(mapping)
                addresses[s.header.p_vaddr] = mapping

        self.mappings = sorted(mappings, key=lambda m: m.start)
        self.addresses = addresses

    def _parse_auxv(self, note):
        t = tube()
        t.unrecv(note.n_desc)

        for i in range(0, note.n_descsz, context.bytes * 2):
            key = t.unpack()
            value = t.unpack()

            # The AT_EXECFN entry is a pointer to the executable's filename
            # at the very top of the stack, followed by a word's with of
            # NULL bytes.  For example, on a 64-bit system...
            #
            # 0x7fffffffefe8  53 3d 31 34  33 00 2f 62  69 6e 2f 62  61 73 68 00  |S=14|3./b|in/b|ash.|
            # 0x7fffffffeff8  00 00 00 00  00 00 00 00                            |....|....|    |    |

            if key == constants.AT_EXECFN:
                self.at_execfn = value
                value = value & ~0xfff
                value += 0x1000
                self.stack = value

    def _parse_stack(self):
        # AT_EXECFN is the start of the filename, e.g. '/bin/sh'
        # Immediately preceding is a NULL-terminated environment variable string.
        # We want to find the beginning of it
        address = self.at_execfn-1

        # Sanity check!
        try:
            assert self.u8(address) == 0
        except AssertionError:
            # Something weird is happening.  Just don't touch it.
            return
        except ValueError:
            # If the stack is not actually present in the coredump, we can't
            # read from the stack.  This will fail as:
            # ValueError: 'seek out of range'
            return

        # Find the next NULL, which is 1 byte past the environment variable.
        while self.u8(address-1) != 0:
            address -= 1

        # We've found the beginning of the last environment variable.
        # We should be able to search up the stack for the envp[] array to
        # find a pointer to this address, followed by a NULL.
        last_env_addr = address
        address &= ~(context.bytes-1)

        while self.unpack(address) != last_env_addr:
            address -= context.bytes

        assert self.unpack(address+context.bytes) == 0

        # We've successfully located the end of the envp[] array.
        # It comes immediately after the argv[] array, which itself
        # is NULL-terminated.
        end_of_envp = address+context.bytes

        while self.unpack(address - context.bytes) != 0:
            address -= context.bytes

        start_of_envp = address

        # Now we can fill in the environment easier.
        for env in range(start_of_envp, end_of_envp, context.bytes):
            envaddr = self.unpack(env)
            value   = self.string(envaddr)
            name    = value.split('=', 1)[0]
            self.env[name] = envaddr

    @property
    def maps(self):
        """A printable string which is similar to /proc/xx/maps."""
        return '\n'.join(map(str, self.mappings))

    def getenv(self, name):
        """getenv(name) -> int

        Read an environment variable off the stack, and return its address.

        Arguments:
            name(str): Name of the environment variable to read.

        Returns:
            The address of the environment variable.
        """
        if name not in self.env:
            log.error("Environment variable %r not set" % name)

        return self.string(self.env[name]).split('=',1)[-1]

    def __getattr__(self, attribute):
        if self.prstatus:
            if hasattr(self.prstatus, attribute):
                return getattr(self.prstatus, attribute)

            if hasattr(self.prstatus.pr_reg, attribute):
                return getattr(self.prstatus.pr_reg, attribute)

        return super(Core, self).__getattribute__(attribute)
