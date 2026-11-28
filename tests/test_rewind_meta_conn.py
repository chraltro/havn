"""The cached Rewind metadata connection (``snapshots._meta_conn_for``).

Its liveness probe must not mistake "busy in another thread" for "closed":
on DuckDB 2 a call on a connection another thread is using is refused, and
the cached connection was replaced while still in use.
"""

from __future__ import annotations

import threading
import time

from havn.engine import snapshots


def test_busy_connection_is_kept_and_a_closed_one_replaced(tmp_path):
    conn = snapshots._meta_conn_for(tmp_path)
    done = threading.Event()

    def busy():
        try:
            conn.execute("SELECT sum(hash(range)) FROM range(20000000000)").fetchall()
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=busy, daemon=True)
    t.start()
    try:
        time.sleep(0.3)  # the query is under way on the connection itself
        assert not done.is_set()
        assert snapshots._conn_is_alive(conn)
        assert snapshots._meta_conn_for(tmp_path) is conn
    finally:
        conn.interrupt()
        t.join(30)

    # Still usable afterwards, and a really closed one is replaced.
    assert snapshots._meta_conn_for(tmp_path) is conn
    conn.close()
    assert not snapshots._conn_is_alive(conn)
    fresh = snapshots._meta_conn_for(tmp_path)
    assert fresh is not conn
    fresh.close()
    snapshots._meta_conns.pop(str(tmp_path.resolve()), None)
