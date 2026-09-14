#!/usr/bin/env python3
"""
I_azure_sql_test.py

A throwaway connectivity check for the Azure SQL Database (server "doniall",
database "EInkDataProcessing") that's meant to eventually receive AIS/other
pipeline data from a Raspberry Pi. Before building any real write/read path
against it, this confirms the whole chain actually works end to end: the
ODBC driver is installed correctly, the admin credentials are right, and
the server's firewall rule allows the machine running this script --
whether that's this Mac today or the Pi later.

It creates a small connection_test table if missing, inserts one row
stamped with the current UTC time and this machine's hostname, then prints
the last 5 rows. Run it twice from two different machines (e.g. once here,
once from the Pi once it's set up) and you should see both hostnames show
up -- that's the real proof the Pi can actually reach this database, not
just that the Portal says the resource exists.

SETUP
    1. Set AZURE_SQL_SERVER (default: doniall.database.windows.net),
       AZURE_SQL_DATABASE (default: EInkDataProcessing), AZURE_SQL_USER,
       AZURE_SQL_PASSWORD as environment variables. Treat the password like
       every other credential in this repo: don't commit it, don't paste it
       into chat.
    2. Make sure the machine running this has a server-level firewall rule
       on "doniall" allowing its current public IP (see
       "az sql server firewall-rule create" -- the Portal's "Add current
       client IP" checkbox does this automatically for whatever machine
       was used to create the server, which won't be the Pi).
    3. pip3 install pyodbc, and the Microsoft ODBC Driver for SQL Server
       (msodbcsql18) -- see this repo's Azure SQL setup notes for the
       apt/driver install steps on Debian/Raspberry Pi OS.

Run:   python3 I_azure_sql_test.py
Needs: pip install pyodbc
"""

import datetime as dt
import os
import socket

import pyodbc

SERVER = os.environ.get("AZURE_SQL_SERVER", "doniall.database.windows.net")
DATABASE = os.environ.get("AZURE_SQL_DATABASE", "EInkDataProcessing")
USER = os.environ.get("AZURE_SQL_USER", "")
PASSWORD = os.environ.get("AZURE_SQL_PASSWORD", "")

# ODBC Driver 18 defaults to Encrypt=yes, which Azure SQL requires anyway --
# spelled out explicitly here so it's obvious this isn't accidentally
# trusting an unverified certificate (TrustServerCertificate stays "no",
# the secure default: verify the server's cert against a real CA)
CONNECTION_STRING = (
    "Driver={ODBC Driver 18 for SQL Server};"
    f"Server=tcp:{SERVER},1433;"
    f"Database={DATABASE};"
    f"Uid={USER};"
    f"Pwd={PASSWORD};"
    "Encrypt=yes;"
    "TrustServerCertificate=no;"
    "Connection Timeout=30;"
)


def main():
    if not (USER and PASSWORD):
        print("AZURE_SQL_USER / AZURE_SQL_PASSWORD not set -- see the setup notes at the "
              "top of this file")
        return

    print(f"connecting to {SERVER}/{DATABASE} as {USER!r}...")
    try:
        conn = pyodbc.connect(CONNECTION_STRING)
    except pyodbc.Error as e:
        print(f"connection failed ({e})")
        print("if this is a login timeout, check the server's firewall rules include this "
              "machine's current public IP -- that's the most common cause")
        return

    with conn:
        cursor = conn.cursor()
        cursor.execute("""
            IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='connection_test' AND xtype='U')
                CREATE TABLE connection_test (
                    id INT IDENTITY PRIMARY KEY,
                    checked_at DATETIME2 NOT NULL,
                    host_note NVARCHAR(200) NOT NULL
                )
        """)
        cursor.execute(
            "INSERT INTO connection_test (checked_at, host_note) VALUES (?, ?)",
            dt.datetime.now(dt.timezone.utc), socket.gethostname(),
        )
        conn.commit()

        cursor.execute("SELECT TOP 5 id, checked_at, host_note FROM connection_test ORDER BY id DESC")
        rows = cursor.fetchall()

    print("connected -- last 5 rows in connection_test (newest first):")
    for row in rows:
        print(f"  id={row.id}  checked_at={row.checked_at}  host_note={row.host_note!r}")


if __name__ == "__main__":
    main()
