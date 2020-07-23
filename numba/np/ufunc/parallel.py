"""
This file implements the code-generator for parallel-vectorize.

ParallelUFunc is the platform independent base class for generating
the thread dispatcher.  This thread dispatcher launches threads
that execute the generated function of UFuncCore.
UFuncCore is subclassed to specialize for the input/output types.
The actual workload is invoked inside the function generated by UFuncCore.
UFuncCore also defines a work-stealing mechanism that allows idle threads
to steal works from other threads.
"""

import os
import sys
import warnings
from threading import RLock as threadRLock
import multiprocessing
from ctypes import CFUNCTYPE, c_int, CDLL, POINTER, c_uint

import numpy as np

import llvmlite.llvmpy.core as lc
import llvmlite.binding as ll

from numba.np.numpy_support import as_dtype
from numba.core import types, config, errors
from numba.np.ufunc.wrappers import _wrapper_info
from numba.np.ufunc import ufuncbuilder
from numba.extending import overload

_IS_OSX = sys.platform.startswith('darwin')
_IS_LINUX = sys.platform.startswith('linux')
_IS_WINDOWS = sys.platform.startswith('win32')


def get_thread_count():
    """
    Gets the available thread count.
    """
    t = config.NUMBA_NUM_THREADS
    if t < 1:
        raise ValueError("Number of threads specified must be > 0.")
    return t


NUM_THREADS = get_thread_count()


def build_gufunc_kernel(library, ctx, info, sig, inner_ndim):
    """Wrap the original CPU ufunc/gufunc with a parallel dispatcher.
    This function will wrap gufuncs and ufuncs something like.

    Args
    ----
    ctx
        numba's codegen context

    info: (library, env, name)
        inner function info

    sig
        type signature of the gufunc

    inner_ndim
        inner dimension of the gufunc (this is len(sig.args) in the case of a
        ufunc)

    Returns
    -------
    wrapper_info : (library, env, name)
        The info for the gufunc wrapper.

    Details
    -------

    The kernel signature looks like this:

    void kernel(char **args, npy_intp *dimensions, npy_intp* steps, void* data)

    args - the input arrays + output arrays
    dimensions - the dimensions of the arrays
    steps - the step size for the array (this is like sizeof(type))
    data - any additional data

    The parallel backend then stages multiple calls to this kernel concurrently
    across a number of threads. Practically, for each item of work, the backend
    duplicates `dimensions` and adjusts the first entry to reflect the size of
    the item of work, it also forms up an array of pointers into the args for
    offsets to read/write from/to with respect to its position in the items of
    work. This allows the same kernel to be used for each item of work, with
    simply adjusted reads/writes/domain sizes and is safe by virtue of the
    domain partitioning.

    NOTE: The execution backend is passed the requested thread count, but it can
    choose to ignore it (TBB)!
    """
    assert isinstance(info, tuple)  # guard against old usage
    # Declare types and function
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)
    byte_ptr_ptr_t = lc.Type.pointer(byte_ptr_t)

    intp_t = ctx.get_value_type(types.intp)
    intp_ptr_t = lc.Type.pointer(intp_t)

    fnty = lc.Type.function(lc.Type.void(), [lc.Type.pointer(byte_ptr_t),
                                             lc.Type.pointer(intp_t),
                                             lc.Type.pointer(intp_t),
                                             byte_ptr_t])
    wrapperlib = ctx.codegen().create_library('parallelgufuncwrapper')
    mod = wrapperlib.create_ir_module('parallel.gufunc.wrapper')
    kernel_name = ".kernel.{}_{}".format(id(info.env), info.name)
    lfunc = mod.add_function(fnty, name=kernel_name)

    bb_entry = lfunc.append_basic_block('')

    # Function body starts
    builder = lc.Builder(bb_entry)

    args, dimensions, steps, data = lfunc.args

    # Release the GIL (and ensure we have the GIL)
    # Note: numpy ufunc may not always release the GIL; thus,
    #       we need to ensure we have the GIL.
    pyapi = ctx.get_python_api(builder)
    gil_state = pyapi.gil_ensure()
    thread_state = pyapi.save_thread()

    def as_void_ptr(arg):
        return builder.bitcast(arg, byte_ptr_t)

    # Array count is input signature plus 1 (due to output array)
    array_count = len(sig.args) + 1

    parallel_for_ty = lc.Type.function(lc.Type.void(),
                                       [byte_ptr_t] * 5 + [intp_t, ] * 3)
    parallel_for = mod.get_or_insert_function(parallel_for_ty,
                                              name='numba_parallel_for')

    # Reference inner-function and link
    innerfunc_fnty = lc.Type.function(
        lc.Type.void(),
        [byte_ptr_ptr_t, intp_ptr_t, intp_ptr_t, byte_ptr_t],
    )
    tmp_voidptr = mod.get_or_insert_function(
        innerfunc_fnty, name=info.name,
    )
    wrapperlib.add_linking_library(info.library)

    get_num_threads = builder.module.get_or_insert_function(
        lc.Type.function(lc.Type.int(types.intp.bitwidth), []),
        name="get_num_threads")

    num_threads = builder.call(get_num_threads, [])

    # Prepare call
    fnptr = builder.bitcast(tmp_voidptr, byte_ptr_t)
    innerargs = [as_void_ptr(x) for x
                 in [args, dimensions, steps, data]]
    builder.call(parallel_for, [fnptr] + innerargs +
                 [intp_t(x) for x in (inner_ndim, array_count)] + [num_threads])

    # Release the GIL
    pyapi.restore_thread(thread_state)
    pyapi.gil_release(gil_state)

    builder.ret_void()

    wrapperlib.add_ir_module(mod)
    wrapperlib.add_linking_library(library)
    return _wrapper_info(library=wrapperlib, name=lfunc.name, env=info.env)


# ------------------------------------------------------------------------------

class ParallelUFuncBuilder(ufuncbuilder.UFuncBuilder):
    def build(self, cres, sig):
        _launch_threads()

        # Buider wrapper for ufunc entry point
        ctx = cres.target_context
        signature = cres.signature
        library = cres.library
        fname = cres.fndesc.llvm_func_name

        info = build_ufunc_wrapper(library, ctx, fname, signature, cres)
        ptr = info.library.get_pointer_to_function(info.name)
        # Get dtypes
        dtypenums = [np.dtype(a.name).num for a in signature.args]
        dtypenums.append(np.dtype(signature.return_type.name).num)
        keepalive = ()
        return dtypenums, ptr, keepalive


def build_ufunc_wrapper(library, ctx, fname, signature, cres):
    innerfunc = ufuncbuilder.build_ufunc_wrapper(library, ctx, fname,
                                                 signature, objmode=False,
                                                 cres=cres)
    info = build_gufunc_kernel(library, ctx, innerfunc, signature,
                               len(signature.args))
    return info

# ---------------------------------------------------------------------------


class ParallelGUFuncBuilder(ufuncbuilder.GUFuncBuilder):
    def __init__(self, py_func, signature, identity=None, cache=False,
                 targetoptions={}):
        # Force nopython mode
        targetoptions.update(dict(nopython=True))
        super(
            ParallelGUFuncBuilder,
            self).__init__(
            py_func=py_func,
            signature=signature,
            identity=identity,
            cache=cache,
            targetoptions=targetoptions)

    def build(self, cres):
        """
        Returns (dtype numbers, function ptr, EnvironmentObject)
        """
        _launch_threads()

        # Build wrapper for ufunc entry point
        info = build_gufunc_wrapper(
            self.py_func, cres, self.sin, self.sout, cache=self.cache,
            is_parfors=False,
        )
        ptr = info.library.get_pointer_to_function(info.name)
        env = info.env

        # Get dtypes
        dtypenums = []
        for a in cres.signature.args:
            if isinstance(a, types.Array):
                ty = a.dtype
            else:
                ty = a
            dtypenums.append(as_dtype(ty).num)

        return dtypenums, ptr, env


# This is not a member of the ParallelGUFuncBuilder function because it is
# called without an enclosing instance from parfors

def build_gufunc_wrapper(py_func, cres, sin, sout, cache, is_parfors):
    """Build gufunc wrapper for the given arguments.
    The *is_parfors* is a boolean indicating whether the gufunc is being
    built for use as a ParFors kernel. This changes codegen and caching
    behavior.
    """
    library = cres.library
    ctx = cres.target_context
    signature = cres.signature
    innerinfo = ufuncbuilder.build_gufunc_wrapper(
        py_func, cres, sin, sout, cache=cache, is_parfors=is_parfors,
    )
    sym_in = set(sym for term in sin for sym in term)
    sym_out = set(sym for term in sout for sym in term)
    inner_ndim = len(sym_in | sym_out)

    info = build_gufunc_kernel(
        library, ctx, innerinfo, signature, inner_ndim,
    )
    return info

# ---------------------------------------------------------------------------


_backend_init_thread_lock = threadRLock()

_windows = sys.platform.startswith('win32')


class _nop(object):
    """A no-op contextmanager
    """

    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass


try:
    # Force the use of an RLock in the case a fork was used to start the
    # process and thereby the init sequence, some of the threading backend
    # init sequences are not fork safe. Also, windows global mp locks seem
    # to be fine.
    if "fork" in multiprocessing.get_start_method() or _windows:
        _backend_init_process_lock = multiprocessing.get_context().RLock()
    else:
        _backend_init_process_lock = _nop()

except OSError as e:

    # probably lack of /dev/shm for semaphore writes, warn the user
    msg = ("Could not obtain multiprocessing lock due to OS level error: %s\n"
           "A likely cause of this problem is '/dev/shm' is missing or"
           "read-only such that necessary semaphores cannot be written.\n"
           "*** The responsibility of ensuring multiprocessing safe access to "
           "this initialization sequence/module import is deferred to the "
           "user! ***\n")
    warnings.warn(msg % str(e))

    _backend_init_process_lock = _nop()

_is_initialized = False

# this is set by _launch_threads
_threading_layer = None


def threading_layer():
    """
    Get the name of the threading layer in use for parallel CPU targets
    """
    if _threading_layer is None:
        raise ValueError("Threading layer is not initialized.")
    else:
        return _threading_layer


def _check_tbb_version_compatible():
    """
    Checks that if TBB is present it is of a compatible version.
    """
    try:
        # first check that the TBB version is new enough
        if _IS_WINDOWS:
            libtbb_name = 'tbb'
        elif _IS_OSX:
            libtbb_name = 'libtbb.dylib'
        elif _IS_LINUX:
            libtbb_name = 'libtbb.so.2'
        else:
            raise ValueError("Unknown operating system")
        libtbb = CDLL(libtbb_name)
        version_func = libtbb.TBB_runtime_interface_version
        version_func.argtypes = []
        version_func.restype = c_int
        tbb_iface_ver = version_func()
        if tbb_iface_ver < 11005: # magic number from TBB
            msg = ("The TBB threading layer requires TBB "
                   "version 2019.5 or later i.e., "
                   "TBB_INTERFACE_VERSION >= 11005. Found "
                   "TBB_INTERFACE_VERSION = %s. The TBB "
                   "threading layer is disabled.")
            problem = errors.NumbaWarning(msg % tbb_iface_ver)
            warnings.warn(problem)
            raise ImportError("Problem with TBB. Reason: %s" % msg)
    except (ValueError, OSError) as e:
        # Translate as an ImportError for consistent error class use, this error
        # will never materialise
        raise ImportError("Problem with TBB. Reason: %s" % e)


def _launch_threads():
    with _backend_init_process_lock:
        with _backend_init_thread_lock:
            global _is_initialized
            if _is_initialized:
                return

            def select_known_backend(backend):
                """
                Loads a specific threading layer backend based on string
                """
                lib = None
                if backend.startswith("tbb"):
                    try:
                        # check if TBB is present and compatible
                        _check_tbb_version_compatible()
                        # now try and load the backend
                        from numba.np.ufunc import tbbpool as lib
                    except ImportError:
                        pass
                elif backend.startswith("omp"):
                    # TODO: Check that if MKL is present that it is a version
                    # that understands GNU OMP might be present
                    try:
                        from numba.np.ufunc import omppool as lib
                    except ImportError:
                        pass
                elif backend.startswith("workqueue"):
                    from numba.np.ufunc import workqueue as lib
                else:
                    msg = "Unknown value specified for threading layer: %s"
                    raise ValueError(msg % backend)
                return lib

            def select_from_backends(backends):
                """
                Selects from presented backends and returns the first working
                """
                lib = None
                for backend in backends:
                    lib = select_known_backend(backend)
                    if lib is not None:
                        break
                else:
                    backend = ''
                return lib, backend

            t = str(config.THREADING_LAYER).lower()
            namedbackends = ['tbb', 'omp', 'workqueue']

            lib = None
            err_helpers = dict()
            err_helpers['TBB'] = ("Intel TBB is required, try:\n"
                                  "$ conda/pip install tbb")
            err_helpers['OSX_OMP'] = ("Intel OpenMP is required, try:\n"
                                      "$ conda/pip install intel-openmp")
            requirements = []

            def raise_with_hint(required):
                errmsg = "No threading layer could be loaded.\n%s"
                hintmsg = "HINT:\n%s"
                if len(required) == 0:
                    hint = ''
                if len(required) == 1:
                    hint = hintmsg % err_helpers[required[0]]
                if len(required) > 1:
                    options = '\nOR\n'.join([err_helpers[x] for x in required])
                    hint = hintmsg % ("One of:\n%s" % options)
                raise ValueError(errmsg % hint)

            if t in namedbackends:
                # Try and load the specific named backend
                lib = select_known_backend(t)
                if not lib:
                    # something is missing preventing a valid backend from
                    # loading, set requirements for hinting
                    if t == 'tbb':
                        requirements.append('TBB')
                    elif t == 'omp' and _IS_OSX:
                        requirements.append('OSX_OMP')
                libname = t
            elif t in ['threadsafe', 'forksafe', 'safe']:
                # User wants a specific behaviour...
                available = ['tbb']
                requirements.append('TBB')
                if t == "safe":
                    # "safe" is TBB, which is fork and threadsafe everywhere
                    pass
                elif t == "threadsafe":
                    if _IS_OSX:
                        requirements.append('OSX_OMP')
                    # omp is threadsafe everywhere
                    available.append('omp')
                elif t == "forksafe":
                    # everywhere apart from linux (GNU OpenMP) has a guaranteed
                    # forksafe OpenMP, as OpenMP has better performance, prefer
                    # this to workqueue
                    if not _IS_LINUX:
                        available.append('omp')
                    if _IS_OSX:
                        requirements.append('OSX_OMP')
                    # workqueue is forksafe everywhere
                    available.append('workqueue')
                else:  # unreachable
                    msg = "No threading layer available for purpose %s"
                    raise ValueError(msg % t)
                # select amongst available
                lib, libname = select_from_backends(available)
            elif t == 'default':
                # If default is supplied, try them in order, tbb, omp,
                # workqueue
                lib, libname = select_from_backends(namedbackends)
                if not lib:
                    # set requirements for hinting
                    requirements.append('TBB')
                    if _IS_OSX:
                        requirements.append('OSX_OMP')
            else:
                msg = "The threading layer requested '%s' is unknown to Numba."
                raise ValueError(msg % t)

            # No lib found, raise and hint
            if not lib:
                raise_with_hint(requirements)

            ll.add_symbol('numba_parallel_for', lib.parallel_for)
            ll.add_symbol('do_scheduling_signed', lib.do_scheduling_signed)
            ll.add_symbol('do_scheduling_unsigned', lib.do_scheduling_unsigned)

            launch_threads = CFUNCTYPE(None, c_int)(lib.launch_threads)
            launch_threads(NUM_THREADS)

            _load_num_threads_funcs(lib)  # load late
            parfor_load_late(lib)

            # set library name so it can be queried
            global _threading_layer
            _threading_layer = libname
            _is_initialized = True


def _load_num_threads_funcs(lib):

    ll.add_symbol('get_num_threads', lib.get_num_threads)
    ll.add_symbol('set_num_threads', lib.set_num_threads)
    ll.add_symbol('get_thread_id', lib.get_thread_id)

    global _set_num_threads
    _set_num_threads = CFUNCTYPE(None, c_int)(lib.set_num_threads)
    _set_num_threads(NUM_THREADS)

    global _get_num_threads
    _get_num_threads = CFUNCTYPE(c_int)(lib.get_num_threads)

    global _get_thread_id
    _get_thread_id = CFUNCTYPE(c_int)(lib.get_thread_id)


# Some helpers to make set_num_threads jittable

def gen_snt_check():
    from numba.core.config import NUMBA_NUM_THREADS
    msg = "The number of threads must be between 1 and %s" % NUMBA_NUM_THREADS

    def snt_check(n):
        if n > NUMBA_NUM_THREADS or n < 1:
            raise ValueError(msg)
    return snt_check


snt_check = gen_snt_check()


@overload(snt_check)
def ol_snt_check(n):
    return snt_check


def set_num_threads(n):
    """
    Set the number of threads to use for parallel execution.

    By default, all :obj:`numba.config.NUMBA_NUM_THREADS` threads are used.

    This functionality works by masking out threads that are not used.
    Therefore, the number of threads *n* must be less than or equal to
    :obj:`~.NUMBA_NUM_THREADS`, the total number of threads that are launched.
    See its documentation for more details.

    This function can be used inside of a jitted function.

    Parameters
    ----------
    n: The number of threads. Must be between 1 and NUMBA_NUM_THREADS.

    See Also
    --------
    get_num_threads, numba.config.NUMBA_NUM_THREADS,
    numba.config.NUMBA_DEFAULT_NUM_THREADS, :envvar:`NUMBA_NUM_THREADS`

    """
    _launch_threads()
    if not isinstance(n, (int, np.integer)):
        raise TypeError("The number of threads specified must be an integer")
    snt_check(n)
    _set_num_threads(n)


@overload(set_num_threads)
def ol_set_num_threads(n):
    _launch_threads()
    if not isinstance(n, types.Integer):
        msg = "The number of threads specified must be an integer"
        raise errors.TypingError(msg)

    def impl(n):
        snt_check(n)
        _set_num_threads(n)
    return impl


def get_num_threads():
    """
    Get the number of threads used for parallel execution.

    By default (if :func:`~.set_num_threads` is never called), all
    :obj:`numba.config.NUMBA_NUM_THREADS` threads are used.

    This number is less than or equal to the total number of threads that are
    launched, :obj:`numba.config.NUMBA_NUM_THREADS`.

    This function can be used inside of a jitted function.

    Returns
    -------
    The number of threads.

    See Also
    --------
    set_num_threads, numba.config.NUMBA_NUM_THREADS,
    numba.config.NUMBA_DEFAULT_NUM_THREADS, :envvar:`NUMBA_NUM_THREADS`

    """
    _launch_threads()
    num_threads = _get_num_threads()
    if num_threads <= 0:
        raise RuntimeError("Invalid number of threads. "
                           "This likely indicates a bug in Numba. "
                           "(thread_id=%s, num_threads=%s)" %
                           (_get_thread_id(), num_threads))
    return num_threads


@overload(get_num_threads)
def ol_get_num_threads():
    _launch_threads()

    def impl():
        num_threads = _get_num_threads()
        if num_threads <= 0:
            print("Broken thread_id: ", _get_thread_id())
            print("num_threads: ", num_threads)
            raise RuntimeError("Invalid number of threads. "
                               "This likely indicates a bug in Numba.")
        return num_threads
    return impl


def _get_thread_id():
    """
    Returns a unique ID for each thread

    This function is private and should only be used for testing purposes.
    """
    _launch_threads()
    return _get_thread_id()


@overload(_get_thread_id)
def ol_get_thread_id():
    _launch_threads()

    def impl():
        return _get_thread_id()
    return impl


_DYLD_WORKAROUND_SET = 'NUMBA_DYLD_WORKAROUND' in os.environ
_DYLD_WORKAROUND_VAL = int(os.environ.get('NUMBA_DYLD_WORKAROUND', 0))

if _DYLD_WORKAROUND_SET and _DYLD_WORKAROUND_VAL:
    _launch_threads()


def parfor_load_late(lib):
    ll.add_symbol('set_parallel_chunksize', lib.set_parallel_chunksize)
    ll.add_symbol('get_parallel_chunksize', lib.get_parallel_chunksize)
    ll.add_symbol('get_sched_size', lib.get_sched_size)
    global _set_parallel_chunksize
    _set_parallel_chunksize = CFUNCTYPE(None, c_uint)(lib.set_parallel_chunksize)
    global _get_parallel_chunksize
    _get_parallel_chunksize = CFUNCTYPE(c_uint)(lib.get_parallel_chunksize)
    global _get_sched_size
    _get_sched_size = CFUNCTYPE(c_uint,
                                c_uint,
                                c_uint,
                                POINTER(c_int),
                                POINTER(c_int)
                      )(lib.get_sched_size)


def set_parallel_chunksize(n):
    _launch_threads()
    if not isinstance(n, (int, np.integer)):
        raise TypeError("The parallel chunkize must be an integer")
    global _set_parallel_chunksize
    _set_parallel_chunksize(n)


def get_parallel_chunksize():
    _launch_threads()
    global _get_parallel_chunksize
    return _get_parallel_chunksize()


@overload(set_parallel_chunksize)
def ol_set_parallel_chunksize(n):
    _launch_threads()
    if not isinstance(n, types.Integer):
        msg = "The parallel chunksize must be an integer"
        raise errors.TypingError(msg)

    def impl(n):
        _set_parallel_chunksize(n)
    return impl

@overload(get_parallel_chunksize)
def ol_get_parallel_chunksize():
    _launch_threads()

    def impl():
        return _get_parallel_chunksize()
    return impl
