#!/usr/bin/env python
#  M Newville <newville@cars.uchicago.edu>
#  The University of Chicago, 2010
#  Epics Open License

"""
  Epics Process Variable
"""
import time
import ctypes
import copy
import functools
from math import log10

from . import ca
from . import dbr
from .utils import is_string

try:
    from types import SimpleNamespace as Namespace
except ImportError:
    from argparse import Namespace

_PVcache_ = {}


def _ensure_context(func):
    '''
    Wrapper that ensures a method is called in the correct CA context

    Assumes the instance has a `context` attribute

    Raises
    ------
    RuntimeError
        If the expected context (self.context) is unset (None), or the current
        thread cannot get a valid context.  Both conditions would normally
        result in a segmentation fault if left unchecked.
    '''
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        initial_context = ca.current_context()
        expected_context = self.context
        if expected_context is None:
            raise RuntimeError('Expected CA context is unset')
        elif expected_context == initial_context:
            return func(self, *args, **kwargs)

        # If not using the expected context, switch to it here:
        if initial_context is not None:
            ca.detach_context()
        ca.attach_context(expected_context)
        try:
            return func(self, *args, **kwargs)
        finally:
            # Then revert back to the initial calling context
            if initial_context is not None:
                ca.detach_context()
                ca.attach_context(initial_context)

    return wrapped


def get_pv(pvname, form='time', connect=False, context=None, timeout=5.0,
           connection_callback=None, access_callback=None, callback=None,
           verbose=False, count=None, auto_monitor=None):
    """
    Get a PV from PV cache or create one if needed.

    Parameters
    ---------
    form : str, optional
        PV form: one of 'native', 'time' (default), 'ctrl'
    connect : bool, optional
        whether to wait for connection (default False)
    context : int, optional
        PV threading context (defaults to current context)
    timeout : float, optional
        connection timeout, in seconds (default 5.0)
    connection_callback : callable, optional
        Called upon connection with keyword arguments: pvname, conn, pv
    access_callback : callable, optional
        Called upon update to access rights with the following signature:
        access_callback(read_access, write_access, pv=epics.PV)
    callback : callable, optional
        Called upon update to change of value.  See `epics.PV.run_callback` for
        further information regarding the signature.
    count : int, optional
        Number of values to request (0 or None means all available values)
    verbose : bool, optional
        Print additional messages relating to PV state
    auto_monitor : bool or epics.dbr.DBE_ flags, optional
        None: auto-monitor if count < ca.AUTOMONITOR_MAXLENGTH
        False: do not auto-monitor
        True: auto-monitor using ca.DEFAULT_SUBSCRIPTION_MASK
        dbr.DBE_*: auto-monitor using this event mask. For example:
                   `epics.dbr.DBE_ALARM|epics.dbr.DBE_LOG`

    Returns
    -------
    pv : epics.PV
    """

    if form not in ('native', 'time', 'ctrl'):
        form = 'native'

    thispv = None
    if context is None:
        context = ca.initial_context
        if context is None:
            context = ca.current_context()

    pvid = (pvname, form, context)
    thispv = _PVcache_.get(pvid, None)

    if thispv is None:
        thispv = default_pv_class(
            pvname, form=form, callback=callback,
            connection_callback=connection_callback,
            access_callback=access_callback, connection_timeout=timeout,
            count=count, verbose=verbose, auto_monitor=auto_monitor)

        # Update the cache with this new instance:
        _PVcache_[pvid] = thispv
    else:
        if connection_callback is not None:
            if thispv.connected:
                connection_callback(pvname=thispv.pvname,
                                    conn=thispv.connected, pv=thispv)
            thispv.connection_callbacks.append(connection_callback)

        if access_callback is not None:
            if thispv.connected:
                access_callback(thispv.read_access, thispv.write_access,
                                pv=thispv)
            thispv.access_callbacks.append(access_callback)

        if callback is not None:
            idx = thispv.add_callback(callback)
            thispv.run_callback(idx)

        if auto_monitor and not thispv.auto_monitor:
            # Start auto-monitoring, if not previously auto-monitoring:
            thispv.auto_monitor = auto_monitor

    if connect:
        if not thispv.wait_for_connection(timeout=timeout):
            ca.write('cannot connect to %s' % pvname)
    return thispv


def fmt_time(tstamp=None):
    "simple formatter for time values"
    if tstamp is None:
        tstamp = time.time()
    tstamp, frac = divmod(tstamp, 1)
    return "%s.%5.5i" % (time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(tstamp)),
                         round(1.e5*frac))


class PV(object):
    """Epics Process Variable

    A PV encapsulates an Epics Process Variable.

    The primary interface methods for a pv are to get() and put() is value::

      >>> p = PV(pv_name)  # create a pv object given a pv name
      >>> p.get()          # get pv value
      >>> p.put(val)       # set pv to specified value.

    Additional important attributes include::

      >>> p.pvname         # name of pv
      >>> p.value          # pv value (can be set or get)
      >>> p.char_value     # string representation of pv value
      >>> p.count          # number of elements in array pvs
      >>> p.type           # EPICS data type: 'string','double','enum','long',..
"""

    _fmtsca = "<PV '%(pvname)s', count=%(count)i, type=%(typefull)s, access=%(access)s>"
    _fmtarr = "<PV '%(pvname)s', count=%(count)i/%(nelm)i, type=%(typefull)s, access=%(access)s>"
    _fields = ('pvname',  'value',  'char_value',  'status',  'ftype',  'chid',
               'host', 'count', 'access', 'write_access', 'read_access',
               'severity', 'timestamp', 'posixseconds', 'nanoseconds',
               'precision', 'units', 'enum_strs',
               'upper_disp_limit', 'lower_disp_limit', 'upper_alarm_limit',
               'lower_alarm_limit', 'lower_warning_limit',
               'upper_warning_limit', 'upper_ctrl_limit', 'lower_ctrl_limit')

    def __init__(self, pvname, callback=None, form='time',
                 verbose=False, auto_monitor=None, count= None,
                 connection_callback=None,
                 connection_timeout=None,
                 access_callback=None):

        self.pvname     = pvname.strip()
        self.form       = form.lower()
        self.verbose    = verbose
        self._auto_monitor = auto_monitor
        self.ftype      = None
        self.connected  = False
        self.connection_timeout = connection_timeout
        self._user_max_count = count

        if self.connection_timeout is None:
            self.connection_timeout = ca.DEFAULT_CONNECTION_TIMEOUT
        self._args      = {}.fromkeys(self._fields)
        self._args['pvname'] = self.pvname
        self._args['count'] = count
        self._args['nelm']  = -1
        self._args['type'] = 'unknown'
        self._args['typefull'] = 'unknown'
        self._args['access'] = 'unknown'
        self.connection_callbacks = []

        if connection_callback is not None:
            self.connection_callbacks = [connection_callback]

        self.access_callbacks = []
        if access_callback is not None:
            self.access_callbacks = [access_callback]

        self.callbacks  = {}
        self._put_complete = None
        self._monref = None  # holder of data returned from create_subscription
        self._monref_mask = None
        self._conn_started = False
        if isinstance(callback, (tuple, list)):
            for i, thiscb in enumerate(callback):
                if hasattr(thiscb, '__call__'):
                    self.callbacks[i] = (thiscb, {})
        elif hasattr(callback, '__call__'):
            self.callbacks[0] = (callback, {})

        self.chid = None
        if ca.current_context() is None:
            ca.use_initial_context()
        self.context = ca.current_context()

        self._args['chid'] = ca.create_channel(self.pvname,
                                               callback=self.__on_connect)
        self.chid = self._args['chid']
        ca.replace_access_rights_event(self.chid,
                                       callback=self.__on_access_rights_event)
        self.ftype  = ca.promote_type(self.chid,
                                      use_ctrl= self.form == 'ctrl',
                                      use_time= self.form == 'time')
        self._args['type'] = dbr.Name(self.ftype).lower()

    @_ensure_context
    def force_connect(self, pvname=None, chid=None, conn=True, **kws):
        if chid is None: chid = self.chid
        if isinstance(chid, ctypes.c_long):
            chid = chid.value
        self._args['chid'] = self.chid = chid
        self.__on_connect(pvname=pvname, chid=chid, conn=conn, **kws)

    @_ensure_context
    def force_read_access_rights(self):
        """force a read of access rights, not relying
        on last event callback.
        Note: event callback seems to fail sometimes,
        at least on initial connection on Windows 64-bit.
        """
        self._args['access'] = ca.access(self.chid)
        self._args['read_access'] = (1 == ca.read_access(self.chid))
        self._args['write_access'] = (1 == ca.write_access(self.chid))

    @_ensure_context
    def __on_access_rights_event(self, read_access, write_access):
        self._args['read_access'] = read_access
        self._args['write_access'] = write_access

        acc = read_access + 2 * write_access
        access_strs = ('no access', 'read-only', 'write-only', 'read/write')
        self._args['access'] = access_strs[acc]

        for cb in self.access_callbacks:
            if callable(cb):
                cb(read_access, write_access, pv=self)

    @_ensure_context
    def __on_connect(self, pvname=None, chid=None, conn=True):
        "callback for connection events"
        # occassionally chid is still None (ie if a second PV is created
        # while __on_connect is still pending for the first one.)
        # Just return here, and connection will happen later
        if self.chid is None and chid is None:
            ca.poll(5.e-4)
            return
        if conn:
            ca.poll()
            self.chid = self._args['chid'] = dbr.chid_t(chid)
            try:
                count = ca.element_count(self.chid)
            except ca.ChannelAccessException:
                time.sleep(0.025)
                count = ca.element_count(self.chid)
            self._args['nelm']  = count

            # allow reduction of elements, via count argument
            self._args['count'] = min(count, self._user_max_count or count)
            self._args['host']  = ca.host_name(self.chid)
            self.ftype = ca.promote_type(self.chid,
                                         use_ctrl= self.form == 'ctrl',
                                         use_time= self.form == 'time')

            _ftype_ = dbr.Name(self.ftype).lower()
            self._args['type'] = _ftype_
            self._args['typefull'] = _ftype_
            self._args['ftype'] = dbr.Name(_ftype_, reverse=True)
            self._check_auto_monitor()

        for conn_cb in self.connection_callbacks:
            if hasattr(conn_cb, '__call__'):
                conn_cb(pvname=self.pvname, conn=conn, pv=self)
            elif not conn and self.verbose:
                ca.write("PV '%s' disconnected." % pvname)

        # pv end of connect, force a read of access rights
        self.force_read_access_rights()

        # waiting until the very end until to set self.connected prevents
        # threads from thinking a connection is complete when it is actually
        # still in progress.
        self.connected = conn

    @_ensure_context
    def _clear_auto_monitor_subscription(self):
        'Clear an auto-monitor subscription, if set'
        if self._monref is None:
            return

        cback, uarg, evid = self._monref

        self._monref = None
        self._monref_mask = None
        ca.clear_subscription(evid)

    @_ensure_context
    def _check_auto_monitor(self):
        '''
        Check the auto-monitor status

        Clears or adds monitor, if necessary.
        '''
        count = self.count
        chid = self.chid

        if count is None or chid is None:
            return

        if self._auto_monitor is None:
            self._auto_monitor = count < ca.AUTOMONITOR_MAXLENGTH

        if not self._auto_monitor:
            # Turn off auto-monitoring, if necessary:
            return self._clear_auto_monitor_subscription()

        mask = (ca.DEFAULT_SUBSCRIPTION_MASK
                if self._auto_monitor is True
                else self._auto_monitor)

        if self._monref is not None:
            if self._monref_mask == mask:
                # Same mask; no need to redo subscription
                return

            # New mask.
            self._clear_auto_monitor_subscription()

        self._monref_mask = mask
        self._monref = ca.create_subscription(
            self.chid,
            use_ctrl=(self.form == 'ctrl'),
            use_time=(self.form == 'time'),
            callback=self.__on_changes,
            mask=mask,
            count=self._user_max_count or 0
        )

    @property
    def auto_monitor(self):
        '''
        Whether auto_monitor is enabled or not. May be one of the following::

            None: auto-monitor if count < ca.AUTOMONITOR_MAXLENGTH
            False: do not auto-monitor
            True: auto-monitor using ca.DEFAULT_SUBSCRIPTION_MASK
            dbr.DBE_*: auto-monitor using this event mask. For example:
                       `epics.dbr.DBE_ALARM|epics.dbr.DBE_LOG`
        '''
        return self._auto_monitor

    @auto_monitor.setter
    @_ensure_context
    def auto_monitor(self, value):
        self._auto_monitor = value
        self._check_auto_monitor()

    @property
    def auto_monitor_mask(self):
        'The current mask in use for auto-monitoring'
        return self._monref_mask

    @_ensure_context
    def wait_for_connection(self, timeout=None):
        """wait for a connection that started with connect() to finish"""
        if not self.connected:
            start_time = time.time()
            if not self._conn_started:
                self.connect(timeout=timeout)

            if not self.connected:
                if timeout is None:
                    timeout = self.connection_timeout
                while not self.connected and time.time()-start_time < timeout:
                    ca.poll()
        return self.connected

    @_ensure_context
    def connect(self, timeout=None):
        "check that a PV is connected, forcing a connection if needed"
        if not self.connected:
            if timeout is None:
                timeout = self.connection_timeout
            ca.connect_channel(self.chid, timeout=timeout)
        self._conn_started = True
        return self.connected and self.ftype is not None

    @_ensure_context
    def clear_auto_monitor(self):
        """turn off auto-monitoring"""
        self.auto_monitor = False

    def reconnect(self):
        "try to reconnect PV"
        self._clear_auto_monitor_subscription()
        self.connected = False
        self._conn_started = False
        self.force_connect()
        return self.wait_for_connection()

    @_ensure_context
    def poll(self, evt=1.e-4, iot=1.0):
        "poll for changes"
        ca.poll(evt=evt, iot=iot)

    def get(self, count=None, as_string=False, as_numpy=True,
            timeout=None, with_ctrlvars=False, use_monitor=True):
        """returns current value of PV.  Use the options:
        count       explicitly limit count for array data
        as_string   flag(True/False) to get a string representation
                    of the value.
        as_numpy    flag(True/False) to use numpy array as the
                    return type for array data.
        timeout     maximum time to wait for value to be received.
                    (default = 0.5 + log10(count) seconds)
        use_monitor flag(True/False) to use value from latest
                    monitor callback (True, default) or to make an
                    explicit CA call for the value.

        >>> get_pv('13BMD:m1.DIR').get()
        0
        >>> get_pv('13BMD:m1.DIR').get(as_string=True)
        'Pos'

        If the Channel Access status code sent by the IOC indicates a failure,
        this method will raise the exception ChannelAccessGetFailure.
        """
        data = self.get_with_metadata(count=count, as_string=as_string,
                                      as_numpy=as_numpy, timeout=timeout,
                                      with_ctrlvars=with_ctrlvars,
                                      use_monitor=use_monitor)
        return (data['value']
                if data is not None
                else None)

    @_ensure_context
    def get_with_metadata(self, count=None, as_string=False, as_numpy=True,
                          timeout=None, with_ctrlvars=False, form=None,
                          use_monitor=True, as_namespace=False):
        """Returns a dictionary of the current value and associated metadata

        count         explicitly limit count for array data
        as_string     flag(True/False) to get a string representation
                      of the value.
        as_numpy      flag(True/False) to use numpy array as the
                      return type for array data.
        timeout       maximum time to wait for value to be received.
                      (default = 0.5 + log10(count) seconds)
        use_monitor   flag(True/False) to use value from latest
                      monitor callback (True, default) or to make an
                      explicit CA call for the value.
        form          {'time', 'ctrl', None} optionally change the type of the
                      get request
        as_namespace  Change the return type to that of a namespace with
                      support for tab-completion

        >>> get_pv('13BMD:m1.DIR', form='time').get_with_metadata()
        {'value': 0, 'status': 0, 'severity': 0}
        >>> get_pv('13BMD:m1.DIR').get_with_metadata(form='ctrl')
        {'value': 0, 'lower_ctrl_limit': 0, ...}
        >>> get_pv('13BMD:m1.DIR').get_with_metadata(as_string=True)
        {'value': 'Pos', 'status': 0, 'severity': 0}
        >>> ns = get_pv('13BMD:m1.DIR').get_with_metadata(as_string=True,
                                                          as_namespace=True)
        >>> ns
        namespace(value='Pos', status=0, severity=0, ...)
        >>> ns.status
        0
        """
        if not self.wait_for_connection(timeout=timeout):
            return None

        if form is None:
            form = self.form
            ftype = self.ftype
        else:
            ftype = ca.promote_type(self.chid,
                                    use_ctrl=(form == 'ctrl'),
                                    use_time=(form == 'time'))

        if with_ctrlvars and getattr(self, 'units', None) is None:
            if form != 'ctrl':
                # ctrlvars will be updated as the get completes, since this
                # metadata comes bundled with our DBR_CTRL* request.
                pass
            else:
                self.get_ctrlvars()

        try:
            cached_length = len(self._args['value'])
        except TypeError:
            cached_length = 1

        if ((not use_monitor) or
                (not self.auto_monitor) or
                (ftype != self.ftype) or
                (self._args['value'] is None) or
                (count is not None and count > cached_length)):

            # respect count argument on subscription also for calls to get
            if count is None and self._args['count']!=self._args['nelm']:
                count = self._args['count']

            # ca.get_with_metadata will handle multiple requests for the same
            # PV internally, so there is no need to change between
            # `get_with_metadata` and `get_complete_with_metadata` here.
            md = ca.get_with_metadata(
                self.chid, ftype=ftype, count=count, timeout=timeout,
                as_numpy=as_numpy)
            if md is None:
                # Get failed. Indicate with a `None` as the return value
                return

            # Update value and all included metadata. Depending on the PV
            # form, this could include timestamp, alarm information,
            # ctrlvars, and so on.
            self._args.update(**md)

            if with_ctrlvars and form != 'ctrl':
                # If the user requested ctrlvars and they were not included in
                # the request, return all metadata.
                md = self._args.copy()

            val = md['value']
        else:
            md = self._args.copy()
            val = self._args['value']

        if as_string:
            char_value = self._set_charval(val, force_long_string=as_string)
            md['value'] = char_value
        elif self.nelm <= 1 or val is None:
            pass
        else:
            # After this point:
            #   * self.nelm is > 1
            #   * val should be set and a sequence
            try:
                len(val)
            except TypeError:
                # Edge case where a scalar value leaks through ca.unpack()
                val = [val]

            if count is None:
                count = len(val)

            if (as_numpy and ca.HAS_NUMPY and
                    not isinstance(val, ca.numpy.ndarray)):
                val = ca.numpy.asarray(val)
            elif (not as_numpy and ca.HAS_NUMPY and
                    isinstance(val, ca.numpy.ndarray)):
                val = val.tolist()

            # allow asking for less data than actually exists in the cached value
            if count < len(val):
                val = val[:count]

            # Update based on the requested type:
            md['value'] = val

        if as_namespace:
            return Namespace(**md)
        return md

    @_ensure_context
    def put(self, value, wait=False, timeout=30.0,
            use_complete=False, callback=None, callback_data=None):
        """set value for PV, optionally waiting until the processing is
        complete, and optionally specifying a callback function to be run
        when the processing is complete.
        """
        if not self.wait_for_connection():
            return None

        if (self.ftype in (dbr.ENUM, dbr.TIME_ENUM, dbr.CTRL_ENUM) and
            is_string(value)):
            if self._args['enum_strs'] is None:
                self.get_ctrlvars()
            if value in self._args['enum_strs']:
                # tuple.index() not supported in python2.5
                # value = self._args['enum_strs'].index(value)
                for ival, val in enumerate(self._args['enum_strs']):
                    if val == value:
                        value = ival
                        break

        def _put_callback(pvname=None, **kws):
            self._put_complete = True
            if callback is not None:
                callback(pvname=pvname, **kws)

        self._put_complete = (False
                              if use_complete
                              else None)

        return ca.put(self.chid, value,
                      wait=wait, timeout=timeout,
                      callback=_put_callback if use_complete or callback else None,
                      callback_data=callback_data)

    def _set_charval(self, val, call_ca=True, force_long_string=False):
        """ sets the character representation of the value.
        intended only for internal use"""
        if val is None:
            self._args['char_value'] = 'None'
            return 'None'
        ftype = self._args['ftype']
        ntype = ca.native_type(ftype)
        if ntype == dbr.STRING:
            self._args['char_value'] = val
            return val
        # char waveform as string
        if ntype == dbr.CHAR and (self.count < ca.AUTOMONITOR_MAXLENGTH or
                force_long_string is True):
            if ca.HAS_NUMPY and isinstance(val, ca.numpy.ndarray):
                # a numpy array
                val = val.tolist()

                if not isinstance(val, list):
                    # a scalar value from numpy, tolist() turns it into a
                    # native python integer
                    val = [val.tolist()]
            else:
                try:
                    # otherwise, try forcing it into a list. this will fail for
                    # scalar types
                    val = list(val)
                except TypeError:
                    # and when it fails, make it a list of one scalar value
                    val = [val]

            if 0 in val:
                firstnull  = val.index(0)
            else:
                firstnull = len(val)
            try:
                cval = ''.join([chr(i) for i in val[:firstnull]]).rstrip()
            except ValueError:
                cval = ''
            self._args['char_value'] = cval
            return cval

        cval  = repr(val)
        if self.count > 1:
            try:
                length = len(val)
            except TypeError:
                length = 1
            cval = '<array size=%d, type=%s>' % (length,
                                                 dbr.Name(ftype).lower())
        elif ntype in (dbr.FLOAT, dbr.DOUBLE):
            if call_ca and self._args['precision'] is None:
                self.get_ctrlvars()
            try:
                prec = self._args['precision']
                fmt  = "%%.%if"
                if 4 < abs(int(log10(abs(val + 1.e-9)))):
                    fmt = "%%.%ig"
                cval = (fmt %  prec) % val
            except (ValueError, TypeError, ArithmeticError):
                cval = str(val)
        elif ntype == dbr.ENUM:
            if call_ca and self._args['enum_strs'] in ([], None):
                self.get_ctrlvars()
            try:
                cval = self._args['enum_strs'][val]
            except (TypeError, KeyError,  IndexError):
                cval = str(val)

        self._args['char_value'] = cval
        return cval

    @_ensure_context
    def get_ctrlvars(self, timeout=5, warn=True):
        "get control values for variable"
        if not self.wait_for_connection():
            return None
        kwds = ca.get_ctrlvars(self.chid, timeout=timeout, warn=warn)
        if kwds is not None:
            self._args.update(kwds)
        self.force_read_access_rights()
        return kwds

    @_ensure_context
    def get_timevars(self, timeout=5, warn=True):
        "get time values for variable"
        if not self.wait_for_connection():
            return None
        kwds = ca.get_timevars(self.chid, timeout=timeout, warn=warn)
        if kwds is not None:
            self._args.update(kwds)
        return kwds


    def __on_changes(self, value=None, **kwd):
        """internal callback function: do not overwrite!!
        To have user-defined code run when the PV value changes,
        use add_callback()
        """
        self._args.update(kwd)
        self._args['value']  = value
        self._args['timestamp'] = kwd.get('timestamp', time.time())
        self._args['posixseconds'] = kwd.get('posixseconds', 0)
        self._args['nanoseconds'] = kwd.get('nanoseconds', 0)
        self._set_charval(self._args['value'], call_ca=False)
        if self.verbose:
            now = fmt_time(self._args['timestamp'])
            ca.write('%s: %s (%s)'% (self.pvname,
                                     self._args['char_value'], now))
        self.run_callbacks()

    @_ensure_context
    def run_callbacks(self):
        """run all user-defined callbacks with the current data

        Normally, this is to be run automatically on event, but
        it is provided here as a separate function for testing
        purposes.
        """
        for index in sorted(list(self.callbacks.keys())):
            self.run_callback(index)

    @_ensure_context
    def run_callback(self, index):
        """run a specific user-defined callback, specified by index,
        with the current data
        Note that callback functions are called with keyword/val
        arguments including:
             self._args  (all PV data available, keys = __fields)
             keyword args included in add_callback()
             keyword 'cb_info' = (index, self)
        where the 'cb_info' is provided as a hook so that a callback
        function  that fails may de-register itself (for example, if
        a GUI resource is no longer available).
        """
        try:
            fcn, kwargs = self.callbacks[index]
        except KeyError:
            return
        kwd = copy.copy(self._args)
        kwd.update(kwargs)
        kwd['cb_info'] = (index, self)
        if hasattr(fcn, '__call__'):
            fcn(**kwd)

    def add_callback(self, callback=None, index=None, run_now=False,
                     with_ctrlvars=True, **kw):
        """add a callback to a PV.  Optional keyword arguments
        set here will be preserved and passed on to the callback
        at runtime.

        Note that a PV may have multiple callbacks, so that each
        has a unique index (small integer) that is returned by
        add_callback.  This index is needed to remove a callback."""
        if hasattr(callback, '__call__'):
            if index is None:
                index = 1
                if len(self.callbacks) > 0:
                    index = 1 + max(self.callbacks.keys())
            self.callbacks[index] = (callback, kw)

        if with_ctrlvars and self.connected:
            self.get_ctrlvars()
        if run_now:
            self.get(as_string=True)
            if self.connected:
                self.run_callback(index)
        return index

    @_ensure_context
    def remove_callback(self, index=None):
        """remove a callback by index"""
        if index in self.callbacks:
            self.callbacks.pop(index)
            ca.poll()

    def clear_callbacks(self):
        "clear all callbacks"
        self.callbacks.clear()

    def _getinfo(self):
        "get information paragraph"
        if not self.wait_for_connection():
            return None
        self.get_ctrlvars()
        out = []
        mod = 'native'
        xtype = self._args['typefull']
        if '_' in xtype:
            mod, xtype = xtype.split('_')

        fmt = '%i'
        if   xtype in ('float','double'):
            fmt = '%g'
        elif xtype in ('string','char'):
            fmt = '%s'

        self._set_charval(self._args['value'], call_ca=False)
        out.append("== %s  (%s_%s) ==" % (self.pvname, mod, xtype))
        if self.count == 1:
            val = self._args['value']
            out.append('   value      = %s' % fmt % val)
        else:
            ext  = {True:'...', False:''}[self.count > 10]
            elems = range(min(5, self.count))
            try:
                aval = [fmt % self._args['value'][i] for i in elems]
            except TypeError:
                aval = ('unknown',)
            out.append("   value      = array  [%s%s]" % (",".join(aval), ext))
        for nam in ('char_value', 'count', 'nelm', 'type', 'units',
                    'precision', 'host', 'access',
                    'status', 'severity', 'timestamp',
                    'posixseconds', 'nanoseconds',
                    'upper_ctrl_limit', 'lower_ctrl_limit',
                    'upper_disp_limit', 'lower_disp_limit',
                    'upper_alarm_limit', 'lower_alarm_limit',
                    'upper_warning_limit', 'lower_warning_limit'):
            if hasattr(self, nam):
                att = getattr(self, nam)
                if att is not None:
                    if nam == 'timestamp':
                        att = "%.3f (%s)" % (att, fmt_time(att))
                    elif nam == 'char_value':
                        att = "'%s'" % att
                    if len(nam) < 12:
                        out.append('   %.11s= %s' % (nam+' '*12, str(att)))
                    else:
                        out.append('   %.20s= %s' % (nam+' '*20, str(att)))
        if xtype == 'enum':  # list enum strings
            out.append('   enum strings: ')
            for index, nam in enumerate(self.enum_strs):
                out.append("       %i = %s " % (index, nam))

        if self._monref is not None:
            msg = 'PV is internally monitored'
            out.append('   %s, with %i user-defined callbacks:' % (msg,
                                                         len(self.callbacks)))
            if len(self.callbacks) > 0:
                for nam in sorted(self.callbacks.keys()):
                    cback = self.callbacks[nam][0]
                    out.append('      %s in file %s' % (cback.func_name,
                                        cback.func_code.co_filename))
        else:
            out.append('   PV is NOT internally monitored')
        out.append('=============================')
        return '\n'.join(out)

    def _getarg(self, arg):
        "wrapper for property retrieval"
        if self._args['value'] is None:
            self.get()
        if self._args[arg] is None:
            if arg in ('status', 'severity', 'timestamp',
                       'posixseconds', 'nanoseconds'):
                self.get_timevars(timeout=1, warn=False)
            else:
                self.get_ctrlvars(timeout=1, warn=False)
        return self._args.get(arg, None)

    def __getval__(self):
        "get value"
        return self._getarg('value')

    def __setval__(self, val):
        "put-value"
        return self.put(val)

    value = property(__getval__, __setval__, None, "value property")

    @property
    def char_value(self):
        "character string representation of value"
        return self._getarg('char_value')

    @property
    def status(self):
        "pv status"
        return self._getarg('status')

    @property
    def type(self):
        "pv type"
        return self._args['type']

    @property
    def typefull(self):
        "pv type"
        return self._args['typefull']

    @property
    def host(self):
        "pv host"
        return self._getarg('host')

    @property
    def count(self):
        """count (number of elements). For array data and later EPICS versions,
        this is equivalent to the .NORD field.  See also 'nelm' property"""
        if self._args['count'] is not None:
            return self._args['count']
        else:
            return self._getarg('count')

    @property
    @_ensure_context
    def nelm(self):
        """native count (number of elements).
        For array data this will return the full array size (ie, the
        .NELM field).  See also 'count' property"""
        # if self._getarg('count') == 1:
        #    return 1
        return ca.element_count(self.chid)

    @property
    def read_access(self):
        "read access"
        return self._getarg('read_access')

    @property
    def write_access(self):
        "write access"
        return self._getarg('write_access')

    @property
    def access(self):
        "read/write access as string"
        return self._getarg('access')

    @property
    def severity(self):
        "pv severity"
        return self._getarg('severity')

    @property
    def timestamp(self):
        "timestamp of last pv action"
        return self._getarg('timestamp')

    @property
    def posixseconds(self):
        """integer seconds for timestamp of last pv action
        using POSIX time convention"""
        return self._getarg('posixseconds')

    @property
    def nanoseconds(self):
        "integer nanoseconds for timestamp of last pv action"
        return self._getarg('nanoseconds')

    @property
    def precision(self):
        "number of digits after decimal point"
        return self._getarg('precision')

    @property
    def units(self):
        "engineering units for pv"
        return self._getarg('units')

    @property
    def enum_strs(self):
        "list of enumeration strings"
        return self._getarg('enum_strs')

    @property
    def upper_disp_limit(self):
        "limit"
        return self._getarg('upper_disp_limit')

    @property
    def lower_disp_limit(self):
        "limit"
        return self._getarg('lower_disp_limit')

    @property
    def upper_alarm_limit(self):
        "limit"
        return self._getarg('upper_alarm_limit')

    @property
    def lower_alarm_limit(self):
        "limit"
        return self._getarg('lower_alarm_limit')

    @property
    def lower_warning_limit(self):
        "limit"
        return self._getarg('lower_warning_limit')

    @property
    def upper_warning_limit(self):
        "limit"
        return self._getarg('upper_warning_limit')

    @property
    def upper_ctrl_limit(self):
        "limit"
        return self._getarg('upper_ctrl_limit')

    @property
    def lower_ctrl_limit(self):
        "limit"
        return self._getarg('lower_ctrl_limit')

    @property
    def info(self):
        "info string"
        return self._getinfo()

    @property
    def put_complete(self):
        "returns True if the last put-with-wait has completed"
        return self._put_complete

    def __repr__(self):
        "string representation"

        if self.connected:
            if self.count == 1:
                return self._fmtsca % self._args
            else:
                return self._fmtarr % self._args
        else:
            return "<PV '%s': not connected>" % self.pvname

    def __eq__(self, other):
        "test for equality"
        try:
            return (self.chid  == other.chid)
        except AttributeError:
            return False

    @_ensure_context
    def disconnect(self):
        "disconnect PV"
        self.connected = False

        ctx = ca.current_context()
        pvid = (self.pvname, self.form, ctx)
        if pvid in _PVcache_:
            _PVcache_.pop(pvid)

        cache_item = ca._cache[ctx].pop(self.pvname, None)
        if cache_item is not None:
            if self._monref is not None:
                # atexit may have already cleared the subscription
                self._clear_auto_monitor_subscription()

            # TODO: clear channel should be called as well
            # ca.clear_channel(cache_item.chid)

        self._monref = None
        self._monref_mask = None
        self.clear_callbacks()
        self._args = {}.fromkeys(self._fields)
        ca.poll(evt=1.e-3, iot=1.0)

    def __del__(self):
        if getattr(ca, 'libca', None) is None:
            return

        try:
            self.disconnect()
        except:
            pass


# Allow advanced users to customize the class of PV that `get_pv` would return:
default_pv_class = PV
