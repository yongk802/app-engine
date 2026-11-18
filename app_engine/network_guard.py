"""Fail-closed TCP port capabilities for Linux isolated units (Landlock ABI 4)."""

from __future__ import annotations

import ctypes
import os
import platform


class _Ruleset(ctypes.Structure):
    _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64)]


class _Port(ctypes.Structure):
    _fields_ = [("access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


class _Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class _Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


def _check(result: int) -> int:
    if result < 0:
        raise OSError(ctypes.get_errno(), "isolated network policy unavailable")
    return result


def _tcp_only(libc) -> None:
    # socket(domain, type, protocol): stream sockets must also specify TCP.
    # SCTP and MPTCP also accept SOCK_STREAM; require TCP explicitly. systemd
    # limits families and denies io_uring, including compatibility architectures.
    number = {"x86_64": 41, "aarch64": 198}.get(platform.machine())
    if number is None:
        raise ValueError("unsupported isolated network architecture")
    instructions = (_Filter * 10)(
        _Filter(0x20, 0, 0, 0),              # load syscall number
        _Filter(0x15, 0, 7, number),         # other syscall -> allow
        _Filter(0x20, 0, 0, 24),            # socket type, args[1]
        _Filter(0x54, 0, 0, 0xF),           # strip CLOEXEC/NONBLOCK
        _Filter(0x15, 0, 3, 1),             # only SOCK_STREAM
        _Filter(0x20, 0, 0, 32),            # socket protocol, args[2]
        _Filter(0x15, 2, 0, 0),             # default TCP protocol
        _Filter(0x15, 1, 0, 6),             # explicit IPPROTO_TCP
        _Filter(0x06, 0, 0, 0x00050001),    # EPERM
        _Filter(0x06, 0, 0, 0x7FFF0000),    # allow
    )
    program = _Program(len(instructions), instructions)
    _check(libc.prctl(22, 2, ctypes.byref(program), 0, 0))


def restrict_network(bind_ports: tuple[int, ...], connect_ports: tuple[int, ...]) -> None:
    """Apply before app code; no downgrade when the kernel lacks a boundary."""
    if platform.system() != "Linux":
        raise ValueError("isolated network policy requires Linux")
    if any(type(port) is not int or not 0 <= port <= 65535
           for port in (*bind_ports, *connect_ports)) or 0 in connect_ports:
        raise ValueError("invalid network capability")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = _check(libc.syscall(444, None, 0, 1))
    if abi < 4:
        raise ValueError("Landlock ABI 4 is required for isolated network policy")
    ruleset = _Ruleset(0, 3)  # handle BIND_TCP and CONNECT_TCP, deny by default
    descriptor = _check(libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0))
    try:
        for access, ports in ((1, bind_ports), (2, connect_ports)):
            for port in ports:
                rule = _Port(access, port)
                _check(libc.syscall(445, descriptor, 2, ctypes.byref(rule), 0))
        _check(libc.prctl(38, 1, 0, 0, 0))  # NO_NEW_PRIVS before both policies
        _check(libc.syscall(446, descriptor, 0))
        _tcp_only(libc)
    finally:
        os.close(descriptor)
