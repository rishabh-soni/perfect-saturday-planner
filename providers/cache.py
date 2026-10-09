"""Shared on-disk TTL cache and cross-process public-service request gates."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

from providers.google import ProviderError


class ProviderCache:
    def __init__(self, path=None, *, clock=time.time, sleep=time.sleep):
        self.path = str(path or os.getenv("PROVIDER_CACHE_PATH") or
                        Path(__file__).resolve().parents[1] / "artifacts" / "provider-cache.sqlite3")
        self.clock, self.sleep = clock, sleep
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS gates (name TEXT PRIMARY KEY, last_start REAL, blocked_until REAL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        except sqlite3.Error:
            raise ProviderError("Provider cache is busy or unavailable; retry later and check its shared file path") from None
        finally:
            db.close()

    @staticmethod
    def key(namespace, data):
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return namespace + ":" + digest

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, self.clock())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value, ttl):
        with self.connect() as db:
            db.execute("DELETE FROM cache WHERE expires<=?", (self.clock(),))
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value, allow_nan=False), self.clock()+ttl))

    def block(self, name, seconds):
        with self.connect() as db:
            db.execute("INSERT INTO gates VALUES (?,0,?) ON CONFLICT(name) DO UPDATE SET blocked_until=MAX(blocked_until,excluded.blocked_until)",
                       (name, self.clock()+seconds))

    def reserve(self, name, *, interval=0):
        # One SQLite writer across every Streamlit session AND local worker process.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT last_start, blocked_until FROM gates WHERE name=?", (name,)).fetchone()
            now = self.clock()
            if row and row[1] > now:
                raise ProviderError(f"{name} is cooling down after an upstream rate limit or failure")
            if row and interval:
                wait = max(0, row[0] + interval - now)
                if wait:
                    self.sleep(wait)
            db.execute("INSERT OR REPLACE INTO gates VALUES (?,?,0)", (name, self.clock()))

    @contextmanager
    def request_gate(self, name, *, interval):
        """Serialize the entire Nominatim request, including cache fill.

        Holding the SQLite writer avoids duplicate concurrent lookups and ensures
        actual requests cannot start too close together after worker scheduling.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT last_start, blocked_until FROM gates WHERE name=?", (name,)).fetchone()
            if row and row[1] > self.clock():
                raise ProviderError(f"{name} is cooling down after an upstream rate limit or failure")
            if row:
                self.sleep(max(0, row[0]+interval-self.clock()))
            db.execute("INSERT OR REPLACE INTO gates VALUES (?,?,0)", (name, self.clock()))
            try:
                yield _TransactionCache(self, db)
            finally:
                # Persist last attempt/cooldown even when the HTTP call fails.
                db.commit()


class _TransactionCache:
    def __init__(self, cache, db):
        self.db, self.clock = db, cache.clock

    def get(self, key):
        row = self.db.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, self.clock())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value, ttl):
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, json.dumps(value, allow_nan=False), self.clock()+ttl))

    def block(self, name, seconds):
        self.db.execute("UPDATE gates SET blocked_until=MAX(blocked_until,?) WHERE name=?", (self.clock()+seconds, name))
