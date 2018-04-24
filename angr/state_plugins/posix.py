import logging
from collections import namedtuple

from .plugin import SimStatePlugin
from .filesystem import SimMount
from ..storage.file import SimFile, SimPacketsStream, Flags, SimFileDescriptor, SimFileDescriptorDuplex
from .. import sim_options as options

l = logging.getLogger("angr.state_plugins.posix")

max_fds = 8192

Stat = namedtuple('Stat', ('st_dev', 'st_ino', 'st_nlink', 'st_mode', 'st_uid',
                           'st_gid', 'st_rdev', 'st_size', 'st_blksize',
                           'st_blocks', 'st_atime', 'st_atimensec', 'st_mtime',
                           'st_mtimensec', 'st_ctime', 'st_ctimensec'))

class PosixDevFS(SimMount): # this'll be mounted at /dev
    def get(self, path):
        if path == ['stdin']:
            return self.state.posix.fd.get(0, None)
        elif path == ['stdout']:
            return self.state.posix.fd.get(1, None)
        elif path == ['stderr']:
            return self.state.posix.fd.get(2, None)
        else:
            return None

    def insert(self, path, simfile): # pylint: disable=unused-argument
        return False

    def delete(self, path): # pylint: disable=unused-argument
        return False

    def merge(self, others, conditions, common_ancestor=None): # pylint: disable=unused-argument
        return False

    def widen(self, others): # pylint: disable=unused-argument
        return False

    def copy(self, _):
        return self # this holds no state!

class SimSystemPosix(SimStatePlugin):
    """
    Data storage and interaction mechanisms for states with an environment conforming to posix.
    Available as ``state.posix``.
    """
    #__slots__ = [ 'maximum_symbolic_syscalls', 'files', 'max_length' ]

    # some posix constants
    SIG_BLOCK=0
    SIG_UNBLOCK=1
    SIG_SETMASK=2

    EPERM      =     1 # /* Operation not permitted */
    ENOENT     =     2 # /* No such file or directory */
    ESRCH      =     3 # /* No such process */
    EINTR      =     4 # /* Interrupted system call */
    EIO        =     5 # /* I/O error */
    ENXIO      =     6 # /* No such device or address */
    E2BIG      =     7 # /* Argument list too long */
    ENOEXEC    =     8 # /* Exec format error */
    EBADF      =     9 # /* Bad file number */
    ECHILD     =    10 # /* No child processes */
    EAGAIN     =    11 # /* Try again */
    ENOMEM     =    12 # /* Out of memory */
    EACCES     =    13 # /* Permission denied */
    EFAULT     =    14 # /* Bad address */
    ENOTBLK    =    15 # /* Block device required */
    EBUSY      =    16 # /* Device or resource busy */
    EEXIST     =    17 # /* File exists */
    EXDEV      =    18 # /* Cross-device link */
    ENODEV     =    19 # /* No such device */
    ENOTDIR    =    20 # /* Not a directory */
    EISDIR     =    21 # /* Is a directory */
    EINVAL     =    22 # /* Invalid argument */
    ENFILE     =    23 # /* File table overflow */
    EMFILE     =    24 # /* Too many open files */
    ENOTTY     =    25 # /* Not a typewriter */
    ETXTBSY    =    26 # /* Text file busy */
    EFBIG      =    27 # /* File too large */
    ENOSPC     =    28 # /* No space left on device */
    ESPIPE     =    29 # /* Illegal seek */
    EROFS      =    30 # /* Read-only file system */
    EMLINK     =    31 # /* Too many links */
    EPIPE      =    32 # /* Broken pipe */
    EDOM       =    33 # /* Math argument out of domain of func */
    ERANGE     =    34 # /* Math result not representable */


    def __init__(self,
            stdin=None,
            stdout=None,
            stderr=None,
            fd=None,
            sockets=None,
            socket_queue=None,
            argv=None,
            argc=None,
            environ=None,
            auxv=None,
            tls_modules=None,
            queued_syscall_returns=None,
            sigmask=None,
            pid=None,
            ppid=None,
            uid=None,
            gid=None,
            brk=None):
        super(SimSystemPosix, self).__init__()

        # some limits and constants
        self.sigmask_bits = 1024
        self.maximum_symbolic_syscalls = 255
        self.max_length = 2 ** 16

        self.argc = argc
        self.argv = argv
        self.environ = environ
        self.auxv = auxv
        self.tls_modules = tls_modules if tls_modules is not None else {}
        self.queued_syscall_returns = [ ] if queued_syscall_returns is None else queued_syscall_returns
        self.brk = brk if brk is not None else 0x1b00000
        self._sigmask = sigmask
        self.pid = 1337 if pid is None else pid
        self.ppid = 1336 if ppid is None else ppid
        self.uid = 1000 if uid is None else uid
        self.gid = 1000 if gid is None else gid
        self.dev_fs = None
        self.autotmp_counter = 0

        self.sockets = sockets if sockets is not None else {}
        self.socket_queue = socket_queue if socket_queue is not None else []

        if stdin is None:
            stdin = SimPacketsStream('stdin', write_mode=False, writable=False, ident='stdin')
        if stdout is None:
            stdout = SimPacketsStream('stdout', write_mode=True, writable=True, ident='stdout')
        if stderr is None:
            stderr = SimPacketsStream('stderr', write_mode=True, writable=True, ident='stderr')

        if fd is None:
            fd = {}
            tty = SimFileDescriptorDuplex(stdin, stdout)

            # the initial fd layout just looks like this:
            # lrwx------ 1 audrey audrey 64 Jan 17 14:21 0 -> /dev/pts/4
            # lrwx------ 1 audrey audrey 64 Jan 17 14:21 1 -> /dev/pts/4
            # lrwx------ 1 audrey audrey 64 Jan 17 14:21 2 -> /dev/pts/4
            # but we want to distinguish the streams. we compromise by having 0 and 1 go to the "tty"
            # and stderr goes to a special stderr file
            fd[0] = tty
            fd[1] = tty
            fd[2] = SimFileDescriptor(stderr, 0)

        self.fd = fd
        # these are the storage mechanisms!
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr

    def init_state(self):
        if self.dev_fs is None:
            self.dev_fs = PosixDevFS()
            self.state.fs.mount("/dev", self.dev_fs)

    def set_brk(self, new_brk):
        # arch word size is not available at init for some reason, fix that here
        if isinstance(self.brk, (int, long)):
            self.brk = self.state.solver.BVV(self.brk, self.state.arch.bits)

        if new_brk.symbolic:
            l.warning("Program is requesting a symbolic brk! This cannot be emulated cleanly!")
            self.brk = self.state.solver.If(new_brk < self.brk, self.brk, new_brk)

        else:
            conc_start = self.state.solver.eval(self.brk)
            conc_end = self.state.solver.eval(new_brk)
            # failure case: new brk is less than old brk
            if conc_end < conc_start:
                pass
            else:
                # set break! we might not actually need to allocate memory though... pages
                self.brk = new_brk

                # if the old and new are in different pages, map
                # we check the byte before each of them since the "break" is the address of the first
                # "unmapped" byte...
                if ((conc_start-1) ^ (conc_end-1)) & ~0xfff:
                    # align up
                    if conc_start & 0xfff:
                        conc_start = (conc_start & ~0xfff) + 0x1000
                    if conc_end & 0xfff:
                        conc_end = (conc_end & ~0xfff) + 0x1000
                    # TODO: figure out what permissions to use
                    self.state.memory.map_region(conc_start, conc_end - conc_start, 7)

        return self.brk

    def set_state(self, state):
        super(SimSystemPosix, self).set_state(state)

        for fd in self.fd:
            self.fd[fd].set_state(state)

        self.stdin.set_state(state)
        self.stdout.set_state(state)
        self.stderr.set_state(state)

    def _pick_fd(self):
        for fd in xrange(0, 8192):
            if fd not in self.fd:
                return fd
        raise SimPosixError('exhausted file descriptors')

    def open(self, name, flags, preferred_fd=None):
        """
        Open a symbolic file. Basically open(2).

        :param name:            Path of the symbolic file, as a string.
        :param flags:           File operation flags, a bitfield of constants from open(2), as an AST
        :param preferred_fd:    Assign this fd if it's not already claimed.
        :return:                The file descriptor number allocated (maps through posix.get_fd to a SimFileDescriptor)
                                or None if the open fails.

        ``mode`` from open(2) is unsupported at present.
        """
        # TODO: speed this up (editor's note: ...really? this is fine)
        fd = None
        if preferred_fd is not None and preferred_fd not in self.fd:
            fd = preferred_fd
        else:
            fd = self._pick_fd()

        flags = self.state.solver.eval(flags)
        writing = (flags & Flags.O_ACCMODE) in (Flags.O_RDWR, Flags.O_WRONLY)

        simfile = self.state.fs.get(name)
        if simfile is None:
            if not writing:
                if not options.ALL_FILES_EXIST:
                    return None
                simfile = SimFile(name, size=self.state.solver.BVS('filesize_%s' % name, self.state.arch.bits, key=('file', name, 'filesize'), eternal=True))
            else:
                simfile = SimFile(name)
            if not self.state.fs.insert(name, simfile):
                return None

        simfd = SimFileDescriptor(simfile, flags)
        simfd.set_state(self.state)
        self.fd[fd] = simfd
        return fd

    def open_socket(self, ident):
        fd = self._pick_fd()

        # we need a sockpair, or a pair of storage mechanisms that will be duplexed to form the socket
        # we can get them from either:
        # a) the socket identifier store
        # b) the socket queue
        # c) making them ourselves
        # in the latter two cases we need to attach them to the socket identifier store

        # control flow sucks. we should be doing our analysis with nothing but mov instructions
        sockpair = None
        if ident not in self.sockets:
            if self.socket_queue:
                sockpair = self.socket_queue.pop(0)
                if sockpair is not None:
                    memo = {}
                    sockpair = sockpair[0].copy(memo), sockpair[1].copy(memo)

            if sockpair is None:
                read_file = SimPacketsStream('socket %s read' % str(ident))
                write_file = SimPacketsStream('socket %s write' % str(ident))
                sockpair = (read_file, write_file)

            self.sockets[ident] = sockpair
        else:
            sockpair = self.sockets[ident]

        simfd = SimFileDescriptorDuplex(sockpair[0], sockpair[1])
        simfd.set_state(self.state)
        self.fd[fd] = simfd
        return fd

    def get_fd(self, fd):
        """
        Looks up the SimFileDescriptor associated with the given number (an AST).
        If the number is concrete and does not map to anything, return None.
        If the number is symbolic, constrain it to an open fd and create a new file for it.
        """
        try:
            fd = self.state.solver.eval_one(fd)
        except SimSolverError:
            ideal = self._pick_fd()
            self.state.solver.add(fd == ideal)
            if not self.state.solver.satisfiable():
                raise SimPosixError("Tried to do operation on symbolic but partially constrained file descriptor")
            fd = ideal
            new_filename = '/tmp/angr_implicit_%d' % self.autotmp_counter
            l.warning("Tried to look up a symbolic fd - constrained to %d and opened %s", ideal, new_filename)
            self.autotmp_counter += 1
            if self.open(new_filename, Flags.O_RDWR, preferred_fd=fd) != fd:
                raise SimPosixError("Something went wrong trying to open implicit temp")

        return self.fd.get(fd)

    def close(self, fd):
        """
        Closes the given file descriptor (an AST).
        Returns whether the operation succeeded (a concrete boolean)
        """
        try:
            fd = self.state.solver.eval_one(fd)
        except SimSolverError:
            l.error("Trying to close a symbolic file descriptor")
            return False

        if fd not in self.fd:
            l.info("Trying to close an unopened file descriptor")
            return False

        del self.fd[fd]
        return True

    def fstat(self, fd): #pylint:disable=unused-argument
        # sizes are AMD64-specific for now
        # TODO: import results from concrete FS, if using concrete FS
        if self.state.solver.symbolic(fd):
            mode = self.state.se.BVS('st_mode', 32, key=('api', 'fstat', 'st_mode'))
        else:
            fd = self.state.se.eval(fd)
            mode = self.state.se.BVS('st_mode', 32, key=('api', 'fstat', 'st_mode')) if fd > 2 else self.state.se.BVV(0, 32)
            # return this weird bogus zero value to keep code paths in libc simple :\

        return Stat(self.state.se.BVV(0, 64), # st_dev
                    self.state.se.BVV(0, 64), # st_ino
                    self.state.se.BVV(0, 64), # st_nlink
                    mode, # st_mode
                    self.state.se.BVV(0, 32), # st_uid (lol root)
                    self.state.se.BVV(0, 32), # st_gid
                    self.state.se.BVV(0, 64), # st_rdev
                    self.state.se.BVS('st_size', 64, key=('api', 'fstat', 'st_size')), # st_size
                    self.state.se.BVV(0, 64), # st_blksize
                    self.state.se.BVV(0, 64), # st_blocks
                    self.state.se.BVV(0, 64), # st_atime
                    self.state.se.BVV(0, 64), # st_atimensec
                    self.state.se.BVV(0, 64), # st_mtime
                    self.state.se.BVV(0, 64), # st_mtimensec
                    self.state.se.BVV(0, 64), # st_ctime
                    self.state.se.BVV(0, 64)) # st_ctimensec

    def sigmask(self, sigsetsize=None):
        """
        Gets the current sigmask. If it's blank, a new one is created (of sigsetsize).

        :param sigsetsize: the size (in *bytes* of the sigmask set)
        :return: the sigmask
        """
        if self._sigmask is None:
            if sigsetsize is not None:
                sc = self.state.se.eval(sigsetsize)
                self.state.add_constraints(sc == sigsetsize)
                self._sigmask = self.state.se.BVS('initial_sigmask', sc*self.state.arch.byte_width, key=('initial_sigmask',), eternal=True)
            else:
                self._sigmask = self.state.se.BVS('initial_sigmask', self.sigmask_bits, key=('initial_sigmask',), eternal=True)
        return self._sigmask

    def sigprocmask(self, how, new_mask, sigsetsize, valid_ptr=True):
        """
        Updates the signal mask.

        :param how: the "how" argument of sigprocmask (see manpage)
        :param new_mask: the mask modification to apply
        :param sigsetsize: the size (in *bytes* of the sigmask set)
        :param valid_ptr: is set if the new_mask was not NULL
        """
        oldmask = self.sigmask(sigsetsize)
        self._sigmask = self.state.se.If(valid_ptr,
            self.state.se.If(how == self.SIG_BLOCK,
                oldmask | new_mask,
                self.state.se.If(how == self.SIG_UNBLOCK,
                    oldmask & (~new_mask),
                    self.state.se.If(how == self.SIG_SETMASK,
                        new_mask,
                        oldmask
                     )
                )
            ),
            oldmask
        )

    @SimStatePlugin.memo
    def copy(self, memo):
        o = SimSystemPosix(
                stdin=self.stdin.copy(memo),
                stdout=self.stdout.copy(memo),
                stderr=self.stderr.copy(memo),
                fd={k: self.fd[k].copy(memo) for k in self.fd},
                sockets={ident: tuple(x.copy(memo) for x in self.sockets[ident]) for ident in self.sockets},
                socket_queue=self.socket_queue, # shouldn't need to copy this - should be copied before use
                argv=self.argv,
                argc=self.argc,
                environ=self.environ,
                auxv=self.auxv,
                tls_modules=self.tls_modules,
                queued_syscall_returns=list(self.queued_syscall_returns),
                sigmask=self._sigmask,
                pid=self.pid,
                ppid=self.ppid,
                uid=self.uid,
                gid=self.gid,
                brk=self.brk)
        o.dev_fs = self.dev_fs.copy(memo)
        return o

    def merge(self, others, merge_conditions, common_ancestor=None):
        for o in others:
            if len(self.fd) != len(o.fd):
                raise SimMergeError("Can't merge states with disparate open file descriptors")
            for fd in self.fd:
                if fd not in o.fd:
                    raise SimMergeError("Can't merge states with disparate open file descriptors")
            if len(self.sockets) != len(o.sockets):
                raise SimMergeError("Can't merge states with disparate sockets")
            for ident in self.sockets:
                if ident not in o.sockets:
                    raise SimMergeError("Can't merge states with disparate sockets")
            if len(self.socket_queue) != len(o.socket_queue) or any(x is not y for x, y in zip(self.socket_queue, o.socket_queue)):
                raise SimMergeError("Can't merge states with disparate socket queues")

        merging_occurred = False
        for fd in self.fd:
            try:
                common_fd = common_ancestor.fd[fd]
            except (AttributeError, KeyError):
                common_fd = None
            merging_occurred |= self.fd[fd].merge(
                    [o.fd[fd] for o in others],
                    merge_conditions,
                    common_ancestor=common_fd)
        for ident in self.sockets:
            try:
                common_sock = common_ancestor.sockets[ident]
            except (AttributeError, KeyError):
                common_sock = None
            merging_occurred |= self.sockets[ident].merge(
                    [o.sockets[ident] for o in others],
                    merge_conditions,
                    common_ancestor=common_sock)

        # pylint: disable=no-member
        # pylint seems to be seriously flipping out here for reasons I'm unsure of
        # it thinks others is a list of bools somehow
        merging_occurred |= self.stdin.merge([o.stdin for o in others], merge_conditions, common_ancestor=common_ancestor.stdin if common_ancestor is not None else None)
        merging_occurred |= self.stdout.merge([o.stdout for o in others], merge_conditions, common_ancestor=common_ancestor.stdout if common_ancestor is not None else None)
        merging_occurred |= self.stderr.merge([o.stderr for o in others], merge_conditions, common_ancestor=common_ancestor.stderr if common_ancestor is not None else None)

        return merging_occurred

    def widen(self, _):
        raise SimMergeError("Widening the system state is unsupported")

    def dump_file_by_path(self, path, **kwargs):
        """
        Returns the concrete content for a file by path.

        :param path: file path as string
        :param kwargs: passed to state.se.eval
        :return: file contents as string
        """
        return self.state.fs.get(path).concretize(**kwargs)

    def dumps(self, fd, **kwargs):
        """
        Returns the concrete content for a file descriptor.

        BACKWARD COMPATIBILITY: if you ask for file descriptors 0 1 or 2, it will return the data from stdin, stdout,
        or stderr as a flat string.

        :param fd:  A file descriptor.
        :return:    The concrete content.
        :rtype:     str
        """
        if 0 <= fd <= 2:
            data = [self.stdin, self.stdout, self.stderr][fd].concretize(**kwargs)
            if type(data) is list:
                data = ''.join(data)
            return data
        return self.get_fd(fd).concretize(**kwargs)


from angr.sim_state import SimState
SimState.register_default('posix', SimSystemPosix)

from ..errors import SimPosixError, SimSolverError, SimMergeError