#!/usr/bin/env python3
# Fire a [loxone ...] target without Klipper.
#
# Reads the same config section the Klipper module reads, so a target that
# works here works in a print. Useful to check credentials and the Virtual
# Text Input name before restarting the printer.
#
# Copyright (C) 2026  Thomas Weirich
#
# This file may be distributed under the terms of the MIT license.
import argparse
import base64
import configparser
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

LOXONE_IO_PATH = "/dev/sps/io/%s/%s"

ALIASES = {
    'loxone_ip': ('loxone_ip', 'loxoneip', 'host'),
    'user': ('user', 'username'),
    'password': ('password', 'passwort'),
    'vi_name': ('vi_name', 'viname'),
    'text_send': ('text_send', 'textsend'),
    'wait_time': ('wait_time', 'waittime'),
}


def pick(section, key, default=None):
    for name in ALIASES.get(key, (key,)):
        # configparser lower-cases option names, exactly like Klipper does.
        if name in section:
            return section[name].split('#')[0].split(';')[0].strip()
    return default


def main():
    ap = argparse.ArgumentParser(
        description="Send one Loxone command from a Klipper config section. "
                    "wait_time is ignored here - this sends straight away, "
                    "because the point is to check the URL and credentials.")
    ap.add_argument('config', help="printer.cfg (or any file holding the "
                                   "[loxone ...] sections)")
    ap.add_argument('name', nargs='?',
                    help="target name; omit to list the configured targets")
    ap.add_argument('--value', help="override text_send")
    ap.add_argument('--method', help="override the HTTP method")
    ap.add_argument('--timeout', type=float, help="override the timeout")
    ap.add_argument('--dry-run', action='store_true',
                    help="print the URL, send nothing")
    args = ap.parse_args()

    parser = configparser.RawConfigParser(strict=False,
                                          inline_comment_prefixes=(';', '#'))
    try:
        with open(args.config, 'r', encoding='utf-8') as fh:
            parser.read_file(fh)
    except OSError as e:
        sys.exit("cannot read %s: %s" % (args.config, e))
    except configparser.Error as e:
        sys.exit("cannot parse %s: %s" % (args.config, e))

    targets = {}
    for raw in parser.sections():
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == 'loxone':
            targets[parts[1].strip()] = parser[raw]
    if not targets:
        sys.exit("no [loxone ...] section found in %s" % (args.config,))
    if args.name is None:
        print("Configured targets in %s:" % (args.config,))
        for name in sorted(targets):
            print("  %s" % (name,))
        return 0
    match = [n for n in targets if n.lower() == args.name.lower()]
    if not match:
        sys.exit("unknown target '%s'; configured: %s"
                 % (args.name, ", ".join(sorted(targets))))
    section = targets[match[0]]

    missing = [k for k in ('loxone_ip', 'user', 'password', 'vi_name')
               if pick(section, k) is None]
    if missing:
        sys.exit("target '%s' is missing: %s" % (match[0], ", ".join(missing)))

    protocol = pick(section, 'protocol', 'http')
    value = args.value or pick(section, 'text_send')
    if value is None:
        sys.exit("target '%s' has no text_send and no --value was given"
                 % (match[0],))
    url = "%s://%s%s" % (
        protocol, pick(section, 'loxone_ip'),
        LOXONE_IO_PATH % (urllib.parse.quote(pick(section, 'vi_name'),
                                             safe='/'),
                          urllib.parse.quote(value, safe='/')))
    method = (args.method or pick(section, 'method', 'POST')).upper()
    timeout = args.timeout
    if timeout is None:
        timeout = float(pick(section, 'timeout', '5'))

    print("%s %s" % (method, url))
    if args.dry_run:
        return 0

    token = base64.b64encode(
        ("%s:%s" % (pick(section, 'user'),
                    pick(section, 'password'))).encode('utf-8'))
    req = urllib.request.Request(
        url, data=None if method in ('GET', 'HEAD') else b"",
        headers={'Authorization': 'Basic ' + token.decode('ascii'),
                 'User-Agent': 'loxklipper'},
        method=method)
    context = None
    verify = pick(section, 'verify_certificate', 'true').lower()
    if protocol == 'https' and verify in ('false', '0', 'no', 'off'):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=context)
    except urllib.error.HTTPError as e:
        body = e.read(512).decode('utf-8', 'replace').strip()
        print("HTTP %s %s%s" % (e.code, e.reason,
                                ("\n" + body) if body else ""))
        if e.code == 401:
            print("-> check user / password, and that the DSM-side user is "
                  "allowed to use the web service")
        elif e.code == 404:
            print("-> the Miniserver does not know a Virtual Text Input "
                  "named '%s'" % (pick(section, 'vi_name'),))
        elif e.code == 405:
            print("-> the Miniserver rejects %s here; try method: GET"
                  % (method,))
        return 1
    except Exception as e:
        print("%s: %s" % (type(e).__name__, e))
        return 1
    body = resp.read(512).decode('utf-8', 'replace').strip()
    resp.close()
    print("HTTP %s%s" % (resp.getcode(), ("\n" + body) if body else ""))
    return 0


if __name__ == '__main__':
    sys.exit(main())
