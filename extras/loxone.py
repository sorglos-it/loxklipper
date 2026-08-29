# Send commands from Klipper G-code to a Loxone Miniserver.
#
# Copyright (C) 2026  Thomas Weirich
#
# This file may be distributed under the terms of the MIT license.
import base64
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Loxone web service endpoint for a Virtual Text Input:
#   <protocol>://<host>/dev/sps/io/<vi_name>/<text_send>
# vi_name is the name of the Virtual Text Input (Virtueller Texteingang) in
# Loxone Config; text_send is the text pushed into it. Downstream in Loxone
# Config that text is compared and turned into a pulse.
LOXONE_IO_PATH = "/dev/sps/io/%s/%s"

# Bytes of the Miniserver reply kept for the console / the log.
MAX_BODY = 512
# How often the blocking wait loop hands control back to the reactor.
WAIT_SLICE = 1.0
# Poll interval while a worker thread is doing the HTTP request.
POLL_SLICE = 0.05
# Pause between retries, inside the worker thread.
RETRY_DELAY = 1.0
# How often the temperature guard re-reads the heaters.
TEMP_POLL = 1.0
# Consecutive readings below the limit before the guard opens. Klipper's
# Heater.get_temp() reports 0.0 when the last sample is more than ~5s old,
# and for a safety guard a stale sensor must not look like a cold nozzle.
# Requiring several readings in a row makes a single stale 0.0 harmless.
TEMP_CONFIRM = 3


def _read_option(config, getter_name, names, **kwargs):
    # Read the first of *names* that the config actually contains.
    # Every alias is probed with config.get() first, because Klipper
    # rejects options that no module ever looked at ("Option 'x' is not
    # valid in section ..."). The loop deliberately does not break early.
    found = None
    for name in names:
        if config.get(name, None) is not None and found is None:
            found = name
    getter = getattr(config, getter_name)
    # Falling back to names[0] gives the canonical option its normal
    # default, or its normal "missing option" error message.
    return getter(names[0] if found is None else found, **kwargs)


class _Pending:
    # One deferred call. It first has to get past the temperature guard
    # (armed=False), and only then does its wait_time start counting.
    def __init__(self, value, wait_time):
        self.value = value
        self.wait_time = wait_time
        self.waketime = 0.
        self.armed = False
        self.confirms = 0


class _Result:
    # Handed to the worker thread, read back once done is set. Nothing else
    # crosses the thread boundary.
    def __init__(self):
        self.done = threading.Event()
        self.status = None
        self.body = ""
        self.error = None
        self.attempts = 0


class LoxoneTarget:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        parts = config.get_name().split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            raise config.error(
                "Section '%s' needs a name, for example [loxone bedroom_tv]"
                % (config.get_name(),))
        self.name = parts[1].strip()
        # Connection. The camelCase spellings are accepted because Klipper
        # lower-cases every option name before a module ever sees it.
        self.host = _read_option(config, 'get',
                                 ['loxone_ip', 'loxoneip', 'host'])
        self.protocol = config.getchoice(
            'protocol', {'http': 'http', 'https': 'https'}, 'http')
        self.verify_certificate = config.getboolean('verify_certificate', True)
        self.user = _read_option(config, 'get', ['user', 'username'])
        self.password = _read_option(config, 'get', ['password', 'passwort'])
        # What is switched. vi_name must match the name of the Virtual Text
        # Input in Loxone Config, character for character.
        self.vi_name = _read_option(config, 'get', ['vi_name', 'viname'])
        self.text_send = _read_option(config, 'get', ['text_send', 'textsend'])
        # Behaviour. wait_time delays the request; it does NOT run after it.
        self.wait_time = _read_option(config, 'getfloat',
                                      ['wait_time', 'waittime'],
                                      default=0., minval=0.)
        self.wait_mode = config.getchoice(
            'wait_mode', {'defer': 'defer', 'block': 'block'}, 'defer')
        self.method = config.get('method', 'POST').strip().upper()
        self.timeout = config.getfloat('timeout', 5., above=0.)
        self.retries = config.getint('retries', 0, minval=0)
        self.on_error = config.getchoice(
            'on_error', {'log': 'log', 'pause': 'pause', 'abort': 'abort'},
            'log')
        # Temperature guard. The request is held back until every configured
        # heater has cooled below its limit; wait_time only starts after that.
        self.temp_limits = []
        nozzle = _read_option(config, 'getfloat', ['nozzle', 'nozzel'],
                              default=None, minval=0.)
        if nozzle is not None:
            self.temp_limits.append(('extruder', nozzle))
        bed = config.getfloat('bed', None, minval=0.)
        if bed is not None:
            self.temp_limits.append(('heater_bed', bed))
        self._heaters = {}
        # Precomputed so the password is turned into a header exactly once
        # and never rebuilt (or logged) per call.
        token = base64.b64encode(
            ("%s:%s" % (self.user, self.password)).encode('utf-8'))
        self._auth_header = "Basic " + token.decode('ascii')
        # Deferred calls waiting to be sent, and requests already in flight.
        # Two long-lived timers drive both, re-armed with update_timer, so a
        # print with many calls does not accumulate dead timers.
        self.pending = []
        self.inflight = []
        self.send_timer = self.reactor.register_timer(self._send_timer)
        self.poll_timer = self.reactor.register_timer(self._poll_timer)
        # Heaters are looked up at ready, not here: [loxone ...] may well be
        # parsed before [extruder]. Failing at ready turns a typo into a
        # startup error instead of a guard that silently never opens.
        if self.temp_limits:
            self.printer.register_event_handler("klippy:ready",
                                                self._handle_ready)
        dispatch = self.printer.lookup_object('loxone_dispatch', None)
        if dispatch is None:
            dispatch = LoxoneDispatch(self.printer)
            self.printer.add_object('loxone_dispatch', dispatch)
        dispatch.add_target(self)

    def build_url(self, value):
        # safe='/' keeps a value that intentionally addresses a deeper path
        # working, while spaces and umlauts in a control name get encoded.
        path = LOXONE_IO_PATH % (
            urllib.parse.quote(self.vi_name, safe='/'),
            urllib.parse.quote(value, safe='/'))
        return "%s://%s%s" % (self.protocol, self.host, path)

    def get_status(self, eventtime=None):
        return {'name': self.name, 'url': self.build_url(self.text_send),
                'method': self.method, 'wait_time': self.wait_time,
                'wait_mode': self.wait_mode, 'pending': len(self.pending),
                'temp_limits': dict(self.temp_limits)}

    def _respond(self, msg, gcmd=None):
        # A deferred send has no G-code command left to answer to, so the
        # reply goes straight to the console instead.
        if gcmd is not None:
            gcmd.respond_info(msg)
        else:
            self.gcode.respond_info(msg)

    # ------------------------------------------------- temperature guard

    def _handle_ready(self):
        # Resolve the heaters now so a wrong name is a startup error. The
        # guard fails safe (it simply never opens), which would otherwise be
        # invisible until the call quietly failed to happen.
        pheaters = self.printer.lookup_object('heaters', None)
        if pheaters is None:
            raise self.printer.config_error(
                "loxone %s: a temperature guard is configured but this "
                "printer has no heaters" % (self.name,))
        for heater_name, limit in self.temp_limits:
            # lookup_heater raises a config error naming the unknown heater
            self._heaters[heater_name] = pheaters.lookup_heater(heater_name)

    def _heater(self, name):
        heater = self._heaters.get(name)
        if heater is None:
            pheaters = self.printer.lookup_object('heaters', None)
            if pheaters is not None:
                try:
                    heater = pheaters.lookup_heater(name)
                    self._heaters[name] = heater
                except Exception:
                    heater = None
        return heater

    def _temp_blocker(self, eventtime):
        # Returns None when every heater is below its limit, otherwise a
        # short reason for the console. Anything unreadable counts as "too
        # hot" so the guard errs towards not sending.
        for heater_name, limit in self.temp_limits:
            heater = self._heater(heater_name)
            if heater is None:
                return "%s not available" % (heater_name,)
            try:
                temp = heater.get_temp(eventtime)[0]
            except Exception as e:
                return "%s unreadable (%s)" % (heater_name, type(e).__name__)
            if temp >= limit:
                return "%s %.1fC >= %.0fC" % (heater_name, temp, limit)
        return None

    def _guard_blocking(self, gcmd):
        confirms = 0
        eventtime = self.reactor.monotonic()
        told = False
        while confirms < TEMP_CONFIRM:
            if self.printer.is_shutdown():
                raise gcmd.error(
                    "loxone %s: temperature guard aborted, printer shut down"
                    % (self.name,))
            blocker = self._temp_blocker(eventtime)
            if blocker is None:
                confirms += 1
            else:
                confirms = 0
                if not told:
                    self._respond("loxone %s: waiting for %s"
                                  % (self.name, blocker), gcmd)
                    told = True
            if confirms >= TEMP_CONFIRM:
                break
            eventtime = self.reactor.pause(eventtime + TEMP_POLL)
        if told:
            self._respond("loxone %s: temperature reached" % (self.name,),
                          gcmd)

    # ---------------------------------------------------------------- HTTP

    def _ssl_context(self):
        if self.protocol != 'https' or self.verify_certificate:
            return None
        # Explicitly asked for by verify_certificate: False - a Miniserver
        # on the LAN normally presents a self-signed certificate.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _http_worker(self, url, result):
        data = None if self.method in ('GET', 'HEAD') else b""
        headers = {'Authorization': self._auth_header,
                   'User-Agent': 'loxklipper'}
        context = self._ssl_context()
        while True:
            result.attempts += 1
            try:
                req = urllib.request.Request(url, data=data, headers=headers,
                                             method=self.method)
                resp = urllib.request.urlopen(req, timeout=self.timeout,
                                              context=context)
                try:
                    result.status = resp.getcode()
                    result.body = resp.read(MAX_BODY).decode(
                        'utf-8', 'replace').strip()
                finally:
                    resp.close()
                result.error = None
                break
            except urllib.error.HTTPError as e:
                result.status = e.code
                result.error = "HTTP %s %s" % (e.code, e.reason)
                try:
                    result.body = e.read(MAX_BODY).decode(
                        'utf-8', 'replace').strip()
                except Exception:
                    result.body = ""
            except Exception as e:
                result.status = None
                result.error = "%s: %s" % (type(e).__name__, e)
            if result.attempts > self.retries:
                break
            time.sleep(RETRY_DELAY)
        result.done.set()

    def _start_worker(self, url, result):
        logging.info("loxklipper: %s %s (target '%s')",
                     self.method, url, self.name)
        thread = threading.Thread(target=self._http_worker,
                                  args=(url, result))
        thread.daemon = True
        thread.start()

    def _report(self, result, url, value, gcmd=None):
        if result.error is not None:
            self._handle_failure(
                "loxone %s: %s %s failed after %d attempt(s): %s"
                % (self.name, self.method, url, result.attempts, result.error),
                gcmd)
            return
        reply = (" %s" % (result.body,)) if result.body else ""
        self._respond("loxone %s: %s -> HTTP %s%s"
                      % (self.name, value, result.status, reply), gcmd)

    def _handle_failure(self, message, gcmd):
        logging.warning("loxklipper: %s", message)
        # 'abort' can only raise while a G-code command is still running. A
        # deferred send has none, so it falls back to pausing.
        if self.on_error == 'abort' and gcmd is not None:
            raise gcmd.error(message)
        self._respond("!! %s" % (message,), gcmd)
        if self.on_error in ('pause', 'abort'):
            if self.printer.lookup_object('pause_resume', None) is None:
                self._respond(
                    "!! loxone %s: on_error is '%s' but [pause_resume] is not "
                    "configured - continuing" % (self.name, self.on_error),
                    gcmd)
                return
            self.gcode.run_script_from_command("PAUSE")

    # ------------------------------------------------- send, blocking path

    def _send_blocking(self, value, gcmd):
        url = self.build_url(value)
        result = _Result()
        self._start_worker(url, result)
        # The socket call must not run in the reactor thread - it would
        # stall MCU communication for the whole timeout and can end in
        # "Timer too close". Poll the worker and let the reactor run.
        eventtime = self.reactor.monotonic()
        budget = (self.timeout + RETRY_DELAY) * (self.retries + 1) + 5.
        deadline = eventtime + budget
        while not result.done.is_set():
            if eventtime > deadline:
                result.error = ("request thread still running after %.0fs"
                                % (budget,))
                break
            eventtime = self.reactor.pause(eventtime + POLL_SLICE)
        self._report(result, url, value, gcmd)

    def _wait_blocking(self, seconds, gcmd):
        eventtime = self.reactor.monotonic()
        endtime = eventtime + seconds
        while eventtime < endtime:
            if self.printer.is_shutdown():
                raise gcmd.error("loxone %s: wait aborted, printer shut down"
                                 % (self.name,))
            eventtime = self.reactor.pause(min(endtime,
                                               eventtime + WAIT_SLICE))

    # ------------------------------------------------ send, deferred path

    def _schedule(self, value, delay, gcmd):
        entry = _Pending(value, delay)
        now = self.reactor.monotonic()
        if self.temp_limits:
            # The guard runs first; wait_time only starts once it opens.
            entry.waketime = now
            blocker = self._temp_blocker(now)
            if blocker is None:
                self._respond("loxone %s: %s waiting for temperature check"
                              % (self.name, value), gcmd)
            else:
                self._respond("loxone %s: %s held back, waiting for %s"
                              % (self.name, value, blocker), gcmd)
        else:
            entry.armed = True
            entry.waketime = now + delay
            self._respond("loxone %s: %s scheduled in %.0fs"
                          % (self.name, value, delay), gcmd)
        self.pending.append(entry)
        self._rearm()

    def _rearm(self):
        if not self.pending:
            self.reactor.update_timer(self.send_timer, self.reactor.NEVER)
            return
        self.reactor.update_timer(self.send_timer,
                                  min(p.waketime for p in self.pending))

    def _send_timer(self, eventtime):
        if self.printer.is_shutdown():
            self.pending = []
            return self.reactor.NEVER
        still = []
        for entry in self.pending:
            if entry.waketime > eventtime:
                still.append(entry)
                continue
            if not entry.armed:
                blocker = self._temp_blocker(eventtime)
                if blocker is not None:
                    entry.confirms = 0
                    entry.waketime = eventtime + TEMP_POLL
                    still.append(entry)
                    continue
                entry.confirms += 1
                if entry.confirms < TEMP_CONFIRM:
                    entry.waketime = eventtime + TEMP_POLL
                    still.append(entry)
                    continue
                entry.armed = True
                if entry.wait_time > 0.:
                    entry.waketime = eventtime + entry.wait_time
                    self._respond(
                        "loxone %s: temperature reached, %s sends in %.0fs"
                        % (self.name, entry.value, entry.wait_time))
                    still.append(entry)
                    continue
                self._respond("loxone %s: temperature reached"
                              % (self.name,))
            url = self.build_url(entry.value)
            result = _Result()
            self._start_worker(url, result)
            self.inflight.append((result, url, entry.value))
        self.pending = still
        if self.inflight:
            self.reactor.update_timer(self.poll_timer,
                                      self.reactor.monotonic() + POLL_SLICE)
        if not self.pending:
            return self.reactor.NEVER
        return min(p.waketime for p in self.pending)

    def _poll_timer(self, eventtime):
        still = []
        for entry in self.inflight:
            result, url, value = entry
            if result.done.is_set():
                self._report(result, url, value, None)
            else:
                still.append(entry)
        self.inflight = still
        if not self.inflight:
            return self.reactor.NEVER
        return eventtime + POLL_SLICE

    # ----------------------------------------------------------- entry point

    def cancel(self):
        count = len(self.pending)
        self.pending = []
        self._rearm()
        return count

    def fire(self, gcmd, value=None, wait_time=None):
        value = self.text_send if value is None else value
        wait_time = self.wait_time if wait_time is None else wait_time
        # Order is: temperature guard, then wait_time, then send. With
        # neither of the two there is nothing to schedule, so the call goes
        # out now and answers in the console.
        if not self.temp_limits and wait_time <= 0.:
            self._send_blocking(value, gcmd)
        elif self.wait_mode == 'block':
            if self.temp_limits:
                self._guard_blocking(gcmd)
            if wait_time > 0.:
                self._wait_blocking(wait_time, gcmd)
            self._send_blocking(value, gcmd)
        else:
            self._schedule(value, wait_time, gcmd)


class LoxoneDispatch:
    # One instance per printer. Owns the G-code commands so that a config
    # with several [loxone ...] sections still registers LOXONE only once.
    def __init__(self, printer):
        self.printer = printer
        self.targets = {}
        gcode = printer.lookup_object('gcode')
        gcode.register_command('LOXONE', self.cmd_LOXONE,
                               desc=self.cmd_LOXONE_help)
        gcode.register_command('LOXONE_LIST', self.cmd_LOXONE_LIST,
                               desc=self.cmd_LOXONE_LIST_help)
        gcode.register_command('LOXONE_CANCEL', self.cmd_LOXONE_CANCEL,
                               desc=self.cmd_LOXONE_CANCEL_help)

    def add_target(self, target):
        key = target.name.upper()
        if key in self.targets:
            raise self.printer.config_error(
                "loxone target '%s' is defined twice (names are compared "
                "case-insensitively)" % (target.name,))
        self.targets[key] = target

    def _lookup(self, gcmd, name):
        target = self.targets.get(name.strip().upper())
        if target is None:
            raise gcmd.error(
                "Unknown loxone target '%s'. Configured: %s"
                % (name, ", ".join(sorted(t.name for t in
                                          self.targets.values())) or "none"))
        return target

    cmd_LOXONE_help = ("Send a command to a Loxone Miniserver: "
                       "LOXONE NAME=<target> [VALUE=<text>] [WAIT=<seconds>]")

    def cmd_LOXONE(self, gcmd):
        target = self._lookup(gcmd, gcmd.get('NAME'))
        target.fire(gcmd,
                    value=gcmd.get('VALUE', None),
                    wait_time=gcmd.get_float('WAIT', None, minval=0.))

    cmd_LOXONE_LIST_help = "List the configured Loxone targets"

    def cmd_LOXONE_LIST(self, gcmd):
        if not self.targets:
            gcmd.respond_info("No [loxone ...] sections configured")
            return
        lines = ["Configured Loxone targets:"]
        for key in sorted(self.targets):
            t = self.targets[key]
            bits = []
            if t.temp_limits:
                bits.append("guard " + ", ".join(
                    "%s<%.0fC" % (n, lim) for n, lim in t.temp_limits))
            if t.wait_time > 0.:
                bits.append("wait %.0fs" % (t.wait_time,))
            bits.append(t.wait_mode if bits else "sends at once")
            if t.pending:
                held = sum(1 for p in t.pending if not p.armed)
                bits.append("%d pending%s"
                            % (len(t.pending),
                               (", %d held by guard" % (held,)) if held else ""))
            lines.append("  %s: %s %s [%s]"
                         % (t.name, t.method, t.build_url(t.text_send),
                            "; ".join(bits)))
        gcmd.respond_info("\n".join(lines))

    cmd_LOXONE_CANCEL_help = ("Drop scheduled Loxone calls that have not been "
                              "sent yet: LOXONE_CANCEL [NAME=<target>]")

    def cmd_LOXONE_CANCEL(self, gcmd):
        name = gcmd.get('NAME', None)
        targets = ([self._lookup(gcmd, name)] if name is not None
                   else list(self.targets.values()))
        total = sum(t.cancel() for t in targets)
        gcmd.respond_info("loxone: cancelled %d scheduled call(s)" % (total,))


def load_config_prefix(config):
    return LoxoneTarget(config)
