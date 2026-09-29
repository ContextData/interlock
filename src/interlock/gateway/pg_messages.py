"""PG Wire Protocol message helpers.

Low-level utilities for reading, writing, and inspecting PostgreSQL
wire-protocol messages.  These work on raw asyncio StreamReader/Writer
pairs and never attempt to interpret full protocol state - that is the
job of ``pg_proxy.PGProxy``.

References:
  https://www.postgresql.org/docs/current/protocol-message-formats.html
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

# ---------------------------------------------------------------------------
# Message type constants (single ASCII character)
# ---------------------------------------------------------------------------

# Frontend (client -> server)
MSG_TYPE_QUERY = "Q"  # Simple query
MSG_TYPE_PARSE = "P"  # Parse (extended query)
MSG_TYPE_BIND = "B"
MSG_TYPE_DESCRIBE = "D"
MSG_TYPE_EXECUTE = "E"
MSG_TYPE_SYNC = "S"
MSG_TYPE_TERMINATE = "X"

# Backend (server -> client)
MSG_TYPE_AUTH = "R"
MSG_TYPE_ROW_DESCRIPTION = "T"
MSG_TYPE_DATA_ROW = "D"
MSG_TYPE_COMMAND_COMPLETE = "C"
MSG_TYPE_READY_FOR_QUERY = "Z"
MSG_TYPE_ERROR_RESPONSE = "E"

# Special protocol codes
SSL_REQUEST_CODE = 80877103
CANCEL_REQUEST_CODE = 80877102
GSSENC_REQUEST_CODE = 80877104
PROTOCOL_VERSION_3_0 = 196608

# Struct formats used repeatedly
_HEADER_STRUCT = struct.Struct("!I")  # 4-byte big-endian unsigned int

DEFAULT_MAX_MESSAGE_PAYLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_STARTUP_PAYLOAD_BYTES = 16 * 1024


class PGProtocolError(ValueError):
    """Base class for malformed or unsupported PG wire protocol input."""


class PGFrameTooLargeError(PGProtocolError):
    """Raised when a PG wire frame exceeds the configured byte limit."""


# ---------------------------------------------------------------------------
# Reading helpers
# ---------------------------------------------------------------------------


async def read_message(
    reader: asyncio.StreamReader,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_MESSAGE_PAYLOAD_BYTES,
) -> tuple[str, bytes]:
    """Read a standard PG wire message (type byte + length + payload).

    Returns ``(msg_type, payload)`` where *payload* does NOT include the
    4-byte length prefix.  Raises ``ConnectionError`` when the peer has
    closed the connection.
    """
    type_byte = await reader.readexactly(1)
    msg_type = chr(type_byte[0])

    length_bytes = await reader.readexactly(4)
    (length,) = _HEADER_STRUCT.unpack(length_bytes)

    # length includes the 4 bytes of itself but not the type byte
    payload_len = length - 4
    if payload_len < 0:
        raise PGProtocolError(f"Invalid message length {length} for type '{msg_type}'")
    if max_payload_bytes is not None and payload_len > max_payload_bytes:
        raise PGFrameTooLargeError(
            f"PG message payload length {payload_len} exceeds limit "
            f"{max_payload_bytes} for type '{msg_type}'"
        )
    payload = await reader.readexactly(payload_len) if payload_len > 0 else b""
    return msg_type, payload


async def read_startup_message(
    reader: asyncio.StreamReader,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_STARTUP_PAYLOAD_BYTES,
) -> tuple[int, bytes]:
    """Read a startup-phase message (no type byte).

    The startup message format is: 4-byte length + payload.
    Returns ``(protocol_or_code, raw_bytes)`` where *raw_bytes* is the
    complete message (length prefix included) suitable for forwarding.
    """
    length_bytes = await reader.readexactly(4)
    (length,) = _HEADER_STRUCT.unpack(length_bytes)

    payload_len = length - 4
    if payload_len < 0:
        raise PGProtocolError(f"Invalid startup message length {length}")
    if max_payload_bytes is not None and payload_len > max_payload_bytes:
        raise PGFrameTooLargeError(
            f"PG startup payload length {payload_len} exceeds limit {max_payload_bytes}"
        )
    payload = await reader.readexactly(payload_len) if payload_len > 0 else b""

    # First 4 bytes of payload are the protocol version or request code
    if len(payload) >= 4:
        (code,) = _HEADER_STRUCT.unpack(payload[:4])
    else:
        code = 0

    raw = length_bytes + payload
    return code, raw


# ---------------------------------------------------------------------------
# Writing helpers
# ---------------------------------------------------------------------------


def pack_message(msg_type: str, payload: bytes) -> bytes:
    """Pack a PG wire message into bytes: type + length + payload."""
    length = len(payload) + 4
    return msg_type.encode("ascii") + _HEADER_STRUCT.pack(length) + payload


def write_message(writer: asyncio.StreamWriter, msg_type: str, payload: bytes) -> None:
    """Write a standard PG wire message (type + length + payload).

    This only buffers - the caller must ``await writer.drain()`` when
    appropriate.
    """
    writer.write(pack_message(msg_type, payload))


# ---------------------------------------------------------------------------
# Payload extraction helpers
# ---------------------------------------------------------------------------


def extract_sql_from_query(payload: bytes) -> str:
    """Extract the SQL string from a SimpleQuery ('Q') payload.

    The payload is the SQL string terminated by ``\\x00``.
    """
    # Strip the trailing NUL
    if payload and payload[-1:] == b"\x00":
        return payload[:-1].decode("utf-8", errors="replace")
    return payload.decode("utf-8", errors="replace")


def extract_sql_from_parse(payload: bytes) -> str:
    """Extract the SQL string from a Parse ('P') payload.

    Parse payload layout:
      - statement name (NUL-terminated string)
      - query string (NUL-terminated string)
      - int16 number of parameter types
      - int32[] parameter type OIDs
    """
    # Skip statement name (first NUL-terminated string)
    nul_pos = payload.find(b"\x00")
    if nul_pos < 0:
        return ""
    rest = payload[nul_pos + 1 :]
    # Extract query string (next NUL-terminated string)
    nul_pos2 = rest.find(b"\x00")
    if nul_pos2 < 0:
        return rest.decode("utf-8", errors="replace")
    return rest[:nul_pos2].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Inspection helpers
# ---------------------------------------------------------------------------


def is_ssl_request(data: bytes) -> bool:
    """Check whether *data* is a complete SSL request message.

    An SSL request is exactly 8 bytes: 4-byte length (8) + 4-byte code
    (80877103).
    """
    if len(data) < 8:
        return False
    (length,) = _HEADER_STRUCT.unpack(data[:4])
    (code,) = _HEADER_STRUCT.unpack(data[4:8])
    return length == 8 and code == SSL_REQUEST_CODE
