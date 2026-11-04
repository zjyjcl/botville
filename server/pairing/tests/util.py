"""Shared helpers for the pairing unit tests (stdlib only)."""

import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


def http_json(method, url, body=None, token=None, headers=None, data=None,
              timeout=10):
    """HTTP request returning (status, decoded body or None).

    HTTP error responses are returned, not raised, so tests can assert on
    errcodes. `data` sends raw bytes verbatim (for signature tests that
    must control the exact body); `body` serialises a JSON object."""
    if data is None and body is not None:
        data = json.dumps(body).encode("utf-8")
    hdrs = {"Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = "Bearer " + token
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None


def raw_request(port, method, path, header_pairs, body=None, timeout=10):
    """Low-level request with full header control (duplicates allowed).

    Returns (status, reason, list-of-(name, value) headers, body bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.putrequest(method, path)
        for key, value in header_pairs:
            conn.putheader(key, value)
        if body is None:
            conn.endheaders()
        else:
            conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, resp.reason, resp.getheaders(), resp.read()
    finally:
        conn.close()


def serve(handler_cls, host="127.0.0.1"):
    """Start a ThreadingHTTPServer on an ephemeral port (daemon thread)."""
    httpd = ThreadingHTTPServer((host, 0), handler_cls)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def base_url(httpd):
    return "http://127.0.0.1:{}".format(httpd.server_address[1])


def shutdown(httpd):
    httpd.shutdown()
    httpd.server_close()
