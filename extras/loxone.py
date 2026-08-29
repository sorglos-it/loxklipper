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
# How often the wait loop hands control back to the reactor.
WAIT_SLICE = 1.0
# Poll interval while the worker thread is doing the HTTP request.
POLL_SLICE = 0.05
# Pause between retries, inside the worker thread.
RETRY_DELAY = 1.0


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


class _Result:
    # Handed to the worker thread, read back by the reactor thread once
    # done is set. Nothing else crosses the thread boundary.
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
        # Behaviour.
        self.wait_time = _read_option(config, 'getfloat',
                                      ['wait_time', 'waittime'],
                                      default=0., minval=0.)
        self.method = config.get('method', 'POST').strip().upper()
        self.timeout = config.getfloat('timeout', 5., above=0.)
        self.retries = config.getint('retries', 0, minval=0)
        self.on_error = config.getchoice(
            'on_error', {'log': 'log', 'pause': 'pause', 'abort': 'abort'},
            'log')
        # Precomputed so the password is turned into a header exactly once
        # and never rebuilt (or logged) per call.
        token = base64.b64encode(
            ("%s:%s" % (self.user, self.password)).encode('utf-8'))
        self._auth_header = "Basic " + token.decode('ascii')
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
                'method': self.method, 'wait_time': self.wait_time}

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

    def _request(self, url):
        result = _Result()
        thread = threading.Thread(target=self._http_worker,
                                  args=(url, result))
        thread.daemon = True
        thread.start()
        # The socket call must not run in the reactor thread - it would
        # stall MCU communication for the whole timeout and can end in
        # "Timer too close". Poll the worker instead and let the reactor
        # keep running in between.
        eventtime = self.reactor.monotonic()
        budget = (self.timeout + RETRY_DELAY) * (self.retries + 1) + 5.
        deadline = eventtime + budget
        while not result.done.is_set():
            if eventtime > deadline:
                result.error = ("request thread still running after %.0fs"
                                % (budget,))
                return result
            eventtime = self.reactor.pause(eventtime + POLL_SLICE)
        return result

    def _wait(self, seconds, gcmd):
        if seconds <= 0.:
            return
        eventtime = self.reactor.monotonic()
        endtime = eventtime + seconds
        while eventtime < endtime:
            if self.printer.is_shutdown():
                raise gcmd.error("loxone %s: wait aborted, printer shut down"
                                 % (self.name,))
            eventtime = self.reactor.pause(min(endtime,
                                               eventtime + WAIT_SLICE))

    def _handle_failure(self, message, gcmd):
        logging.warning("loxklipper: %s", message)
        if self.on_error == 'abort':
            raise gcmd.error(message)
        gcmd.respond_info("!! %s" % (message,))
        if self.on_error == 'pause':
            if self.printer.lookup_object('pause_resume', None) is None:
                gcmd.respond_info(
                    "!! loxone %s: on_error is 'pause' but [pause_resume] is "
                    "not configured - continuing" % (self.name,))
                return
            self.gcode.run_script_from_command("PAUSE")

    def fire(self, gcmd, value=None, wait_time=None):
        value = self.text_send if value is None else value
        wait_time = self.wait_time if wait_time is None else wait_time
        url = self.build_url(value)
        logging.info("loxklipper: %s %s (target '%s')",
                     self.method, url, self.name)
        result = self._request(url)
        if result.error is not None:
            self._handle_failure(
                "loxone %s: %s %s failed after %d attempt(s): %s"
                % (self.name, self.method, url, result.attempts, result.error),
                gcmd)
        else:
            reply = (" %s" % (result.body,)) if result.body else ""
            gcmd.respond_info("loxone %s: %s -> HTTP %s%s"
                              % (self.name, value, result.status, reply))
        self._wait(wait_time, gcmd)


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

    def add_target(self, target):
        key = target.name.upper()
        if key in self.targets:
            raise self.printer.config_error(
                "loxone target '%s' is defined twice (names are compared "
                "case-insensitively)" % (target.name,))
        self.targets[key] = target

    cmd_LOXONE_help = ("Send a command to a Loxone Miniserver: "
                       "LOXONE NAME=<target> [VALUE=<text>] [WAIT=<seconds>]")

    def cmd_LOXONE(self, gcmd):
        name = gcmd.get('NAME')
        target = self.targets.get(name.strip().upper())
        if target is None:
            raise gcmd.error(
                "Unknown loxone target '%s'. Configured: %s"
                % (name, ", ".join(sorted(t.name for t in
                                          self.targets.values())) or "none"))
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
            lines.append("  %s: %s %s (wait %.0fs)"
                         % (t.name, t.method, t.build_url(t.text_send),
                            t.wait_time))
        gcmd.respond_info("\n".join(lines))


def load_config_prefix(config):
    return LoxoneTarget(config)
