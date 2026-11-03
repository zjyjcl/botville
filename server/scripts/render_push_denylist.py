#!/usr/bin/env python3
"""Render the tuwunel/continuwuity push denylist env file (A1.2).

Both homeservers refuse outbound HTTP requests to private address ranges by
default (SSRF protection), which would also block pushes to the sygnal push
gateway living in the compose network. server/scripts/init.sh runs this
script to render server/compose/generated/push-denylist.env: the FULL
upstream default denylist, minus exactly the sygnal container's single /32
(BOTVILLE_SYGNAL_IP) when one is configured. TUWUNEL_IP_RANGE_DENYLIST /
CONTINUWUITY_IP_RANGE_DENYLIST replace the default entirely (they are not
appended to), so the generated list must keep every upstream entry except
the allowed /32 — it is kept identical to tuwunel v1.9.4's
default_ip_range_denylist() (src/core/config/mod.rs), which includes
"0.0.0.0/8" and "::/128"; an earlier revision of this generator omitted
those two and silently re-allowed them.

With BOTVILLE_SYGNAL_IP empty nothing is carved out: the denylist is the
untouched upstream default, for production where sygnal sits behind a
public HTTPS domain and the SSRF protection should stay fully closed.

Validation (friendly one-line error, exit 1, no traceback):
  - BOTVILLE_DOCKER_SUBNET: RFC1918 IPv4 CIDR, prefix >= /24, no host bits;
  - BOTVILLE_DOCKER_IP_RANGE: the compose ipam dynamic pool, a CIDR inside
    the subnet (unset only works for the default subnet);
  - BOTVILLE_SYGNAL_IP (when set): IPv4 inside the subnet, not the network,
    gateway (.1) or broadcast address, and outside the dynamic pool.

Usage: python3 render_push_denylist.py <env_file> <out_file>
Unit tests: server/tests/test_push_denylist.py (standard library only).
"""

import ipaddress
import sys

# Matches the ${BOTVILLE_DOCKER_SUBNET:-...} default in docker-compose.yml;
# only used when the key is missing from .env entirely (init.sh appends
# missing keys from .env.example, so this is a last-resort fallback).
DEFAULT_SUBNET = "172.31.250.0/24"

# Dynamic-address pool for the compose network (ipam ip_range, same default
# as docker-compose.yml): docker's IPAM allocates dynamically from the start
# of the subnet (.2, .3, ...), which would race the sygnal container's fixed
# .2 — whichever container starts first would win and the other fail. The
# pool keeps dynamic addresses in .64-.127 so BOTVILLE_SYGNAL_IP (.2 by
# convention, any host address outside the pool works) is never contested.
DEFAULT_IP_RANGE = "172.31.250.64/26"

# tuwunel v1.9.4 default ip_range_denylist, order preserved; continuwuity
# ships the same set. 0.0.0.0/8 and ::/128 are load-bearing (review B1).
UPSTREAM_DEFAULT_DENYLIST = [
    "127.0.0.0/8",
    "0.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "192.0.0.0/24",
    "169.254.0.0/16",
    "192.88.99.0/24",
    "198.18.0.0/15",
    "192.0.2.0/24",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "::1/128",
    "::/128",
    "fe80::/10",
    "fc00::/7",
    "2001:db8::/32",
    "ff00::/8",
    "fec0::/10",
]

RFC1918 = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]


class ConfigError(Exception):
    """Invalid .env value; main() prints the message without a traceback."""


def load_env_values(path):
    """KEY=VALUE lines of an env file; the last assignment of a key wins."""
    values = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key:
                values[key] = value
    return values


def parse_subnet(value):
    """Validate BOTVILLE_DOCKER_SUBNET and return it as an IPv4Network."""
    if "/" not in value:
        raise ConfigError(
            "BOTVILLE_DOCKER_SUBNET={!r} must be a CIDR network with an explicit "
            "prefix (e.g. 172.31.250.0/24)".format(value)
        )
    try:
        subnet = ipaddress.ip_network(value, strict=True)
    except ValueError:
        hint = ""
        try:
            loose = ipaddress.ip_network(value.strip(), strict=False)
            if str(loose) != value.strip():
                hint = " (host bits set? use {})".format(loose)
        except ValueError:
            pass
        raise ConfigError(
            "BOTVILLE_DOCKER_SUBNET={!r} is not a valid CIDR network{}".format(value, hint)
        )
    if subnet.version != 4:
        raise ConfigError(
            "BOTVILLE_DOCKER_SUBNET={} must be IPv4 "
            "(the compose network is IPv4-only)".format(subnet)
        )
    if not any(subnet.subnet_of(net) for net in RFC1918):
        raise ConfigError(
            "BOTVILLE_DOCKER_SUBNET={} is not inside RFC1918 private space "
            "(10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)".format(subnet)
        )
    if subnet.prefixlen < 24:
        raise ConfigError(
            "BOTVILLE_DOCKER_SUBNET={} is too broad; use a prefix of /24 or "
            "longer (e.g. 172.31.250.0/24) so the push allow never covers "
            "more than this stack's own network".format(subnet)
        )
    return subnet


def parse_ip_range(value, subnet):
    """Validate BOTVILLE_DOCKER_IP_RANGE (compose ipam dynamic pool).

    Unset/empty falls back to DEFAULT_IP_RANGE, which only fits the default
    subnet — with a custom subnet the key must name a range inside it.
    """
    if not value:
        value = DEFAULT_IP_RANGE
        if str(subnet) != DEFAULT_SUBNET:
            raise ConfigError(
                "BOTVILLE_DOCKER_IP_RANGE is unset and BOTVILLE_DOCKER_SUBNET={} "
                "is not the default; set it to a CIDR inside the subnet (e.g. "
                "{}, matching how the .64/26 default sits inside "
                "{})".format(subnet, _pool_hint(subnet), DEFAULT_SUBNET)
            )
    try:
        ip_range = ipaddress.ip_network(value, strict=True)
    except ValueError:
        raise ConfigError(
            "BOTVILLE_DOCKER_IP_RANGE={!r} is not a valid CIDR network "
            "(host bits set? use the network address)".format(value)
        )
    if ip_range.version != 4 or not ip_range.subnet_of(subnet):
        raise ConfigError(
            "BOTVILLE_DOCKER_IP_RANGE={} is not inside BOTVILLE_DOCKER_SUBNET="
            "{}; use a pool inside the subnet, e.g. {}".format(
                ip_range, subnet, _pool_hint(subnet)
            )
        )
    return ip_range


def _pool_hint(subnet):
    """A plausible dynamic pool for a custom subnet, for error messages.

    Mirrors the default layout (a /26 slice clear of the .2 static address):
    the .64/26 slice for subnets with room for it, otherwise the upper half.
    """
    if subnet.prefixlen <= 25:  # 128+ addresses: the .64/26 slice fits
        return "{}/26".format(subnet.network_address + 64)
    halves = list(subnet.subnets(new_prefix=subnet.prefixlen + 1))
    return str(halves[-1])


def parse_sygnal_ip(value, subnet, ip_range):
    """Validate BOTVILLE_SYGNAL_IP; empty -> None (no private range allowed)."""
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise ConfigError(
            "BOTVILLE_SYGNAL_IP={!r} is not a valid IP address".format(value)
        )
    if ip.version != 4:
        raise ConfigError("BOTVILLE_SYGNAL_IP={} must be IPv4".format(ip))
    if ip not in subnet:
        raise ConfigError(
            "BOTVILLE_SYGNAL_IP={} is outside BOTVILLE_DOCKER_SUBNET={}".format(ip, subnet)
        )
    reserved = [
        (subnet.network_address, "the network address"),
        (subnet.network_address + 1, "the gateway address (.1)"),
        (subnet.broadcast_address, "the broadcast address"),
    ]
    for bad, what in reserved:
        if ip == bad:
            raise ConfigError(
                "BOTVILLE_SYGNAL_IP={} is {}; pick a free host address "
                "such as {}".format(ip, what, subnet.network_address + 2)
            )
    if ip in ip_range:
        raise ConfigError(
            "BOTVILLE_SYGNAL_IP={} is inside the dynamic address pool "
            "(BOTVILLE_DOCKER_IP_RANGE={}); docker would race the sygnal "
            "container for it. Pick a static address outside the pool (the "
            "network's .2 by convention)".format(ip, ip_range)
        )
    return ip


def render_denylist(sygnal_ip):
    """Upstream default denylist with the sygnal /32 carved out (if any)."""
    allowed = None
    if sygnal_ip is not None:
        allowed = ipaddress.ip_network("{}/32".format(sygnal_ip))
    out = []
    for cidr in UPSTREAM_DEFAULT_DENYLIST:
        net = ipaddress.ip_network(cidr)
        if allowed is not None and net.version == allowed.version and allowed.subnet_of(net):
            out.extend(str(n) for n in net.address_exclude(allowed))
        else:
            out.append(cidr)
    return out


def render_file_text(subnet, ip_range, sygnal_ip):
    denylist = render_denylist(sygnal_ip)
    value = "[" + ", ".join('"{}"'.format(c) for c in denylist) + "]"
    lines = [
        "# Generated by server/scripts/render_push_denylist.py via init.sh; do not edit.",
        "# BOTVILLE_DOCKER_SUBNET={} (dynamic pool {})".format(subnet, ip_range),
    ]
    if sygnal_ip is not None:
        lines.append("# Push allow: the sygnal container's /32 only ({}/32); every other".format(sygnal_ip))
        lines.append("# upstream-default private range stays denied.")
    else:
        lines.append("# BOTVILLE_SYGNAL_IP is empty: no private range is allow-listed")
        lines.append("# (full upstream default denylist; public push gateway).")
    lines.append("TUWUNEL_IP_RANGE_DENYLIST={}".format(value))
    lines.append("CONTINUWUITY_IP_RANGE_DENYLIST={}".format(value))
    return "\n".join(lines) + "\n"


def main(argv):
    if len(argv) != 3:
        print("usage: render_push_denylist.py <env_file> <out_file>", file=sys.stderr)
        return 2
    env_path, out_path = argv[1:3]
    try:
        values = load_env_values(env_path)
        subnet = parse_subnet(values.get("BOTVILLE_DOCKER_SUBNET") or DEFAULT_SUBNET)
        ip_range = parse_ip_range(values.get("BOTVILLE_DOCKER_IP_RANGE", ""), subnet)
        sygnal_ip = parse_sygnal_ip(
            values.get("BOTVILLE_SYGNAL_IP", ""), subnet, ip_range
        )
        text = render_file_text(subnet, ip_range, sygnal_ip)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    except (ConfigError, OSError) as exc:
        print("init.sh: render_push_denylist: {}".format(exc), file=sys.stderr)
        return 1
    if sygnal_ip is not None:
        print(
            "init.sh: rendered {} (push allowed to {}/32 only)".format(out_path, sygnal_ip)
        )
    else:
        print(
            "init.sh: rendered {} (no private range allowed; full default denylist)".format(
                out_path
            )
        )
        if str(subnet) != DEFAULT_SUBNET:
            # With the IP empty, compose falls back to the hard-coded
            # ipv4_address default below, which only fits the default subnet.
            print(
                "WARNING: BOTVILLE_SYGNAL_IP is empty (push allow disabled) but "
                "BOTVILLE_DOCKER_SUBNET={} is not the default; docker-compose.yml's "
                "fallback sygnal ipv4_address 172.31.250.2 is outside it and the "
                "push profile will fail to start. Set BOTVILLE_SYGNAL_IP to a free "
                "address in the subnet (which also re-enables the allow) or keep "
                "sygnal outside this stack.".format(subnet),
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
