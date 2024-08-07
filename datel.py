import json
import logging
import os
import ssl
import sqlite3
import time
import datetime as dt
import zoneinfo
from urllib.request import urlopen

SQLITE_TRAINS = """
CREATE TABLE vlaky (
    vlozeno TIMESTAMP NOT NULL,
    aktualizovano TIMESTAMP NOT NULL,
    cislo TEXT NOT NULL,
    nazev TEXT NOT NULL,
    provozovatel TEXT NOT NULL,
    datum_odjezd TEXT NOT NULL,
    stanice_vychozi TEXT NOT NULL,
    stanice_cilova TEXT NOT NULL,
    ocekavany_odjezd TIMESTAMP NOT NULL,
    realny_odjezd TIMESTAMP NOT NULL,
    ocekavany_prijezd TIMESTAMP,
    realny_prijezd TIMESTAMP,
    UNIQUE(cislo, nazev, provozovatel, datum_odjezd)
)
"""
# delka_cesty_minut INT NOT NULL, -- TODO: generated always as (stejne jako dalsi dva)
# zpozdeni_odjezd INT,
# zpozdeni_prijezd INT,


# e.g. 15:03 -> 2023-01-30 15:03 or 2023-01-29 15:03 (whichever is more likely depending on `now`)
def datetime_from_stringtime(tm, now):
    today = now.date()
    yesterday = today - dt.timedelta(days=1)
    tomorrow = today + dt.timedelta(days=1)
    for day in [today, yesterday, tomorrow]:
        hour, _, minute = tm.partition(":")
        cmb = dt.datetime.combine(
            day, dt.time(hour=int(hour), minute=int(minute)), tzinfo=SZ_TZ
        )
        diff = max(now, cmb) - min(now, cmb)
        if diff.total_seconds() < 8 * 3600:
            return cmb

    # raise ValueError(f"cannot match time to day: {tm}")
    return None


URL = r"https://mapy.spravazeleznic.cz/serverside/request2.php?module=Layers\OsVlaky&&action=load"
SZ_TZ = zoneinfo.ZoneInfo("Europe/Prague")

FETCH_EVERY = dt.timedelta(seconds=15)

if __name__ == "__main__":
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    logging.getLogger().setLevel(logging.INFO)

    dbfile = "prehled.db"
    dbexists = os.path.isfile(dbfile)
    conn = sqlite3.connect(dbfile)
    if not dbexists:
        conn.execute(SQLITE_TRAINS)

    run = True
    is_ci = is_ci = os.environ.get("CI") is not None
    last_fetch = dt.datetime.now() - dt.timedelta(hours=1)

    while run:
        if is_ci:
            logging.info("Spoustim v CI, koncim po jednom kole")
            run = False

        delta = dt.datetime.now() - last_fetch
        if delta < FETCH_EVERY:
            logging.info(
                "Naposledy nacteno %s, cekam %s do dalsiho ticku",
                last_fetch,
                FETCH_EVERY - delta,
            )
            time.sleep((FETCH_EVERY - delta).total_seconds())

        last_fetch = dt.datetime.now()
        with urlopen(URL, context=ctx) as rr:
            data = json.load(rr)
            # data = json.load(open("payload.json"))
            assert data["success"]
            logging.info("Mame %s vlaku v pohybu", len(data["result"]))

            now = dt.datetime.now(SZ_TZ)
            for el in data["result"]:
                props = el["properties"]
                if props["type"] != "V":
                    logging.info(
                        "preskakujeme zaznam, ma neznamy typ: %s", props["type"]
                    )

                train_no = props["tt"] + " " + props["tn"]  # e.g. EC + 332
                train_name = props["na"]
                dep_st = props["fn"]
                dest_st = props["ln"]
                latest_st = props["cna"]
                carrier = props["d"]
                delay = props["de"]
                planned_time = datetime_from_stringtime(props["cp"], now=now)
                real_time = datetime_from_stringtime(props["cr"], now=now)
                if planned_time is None or real_time is None:
                    # TODO: loguj tohle do JSON a inspektuj - tady je nejaka divna vec, kdy nam to
                    # hlasi stary vlak - nebo nejakej, co jel dlouho?
                    logging.info("Problem s casem: %s %s", planned_time, real_time)
                    continue

                departure_planned, departure_real = planned_time, real_time
                arrival_planned, arrival_real = planned_time, real_time

                # only departures and arrivals
                if not ((latest_st == dep_st) or (latest_st == dest_st)):
                    continue

                # TODO: vlak dojel v 00:30, jak pozname, jestli vyjel po pulnoci nebo pred ni?
                # v tuhle chvili se divame na posledni zaznam v datech
                if latest_st == dest_st:
                    # UNIQUE(cislo, nazev, provozovatel, datum_odjezd)
                    last = conn.execute(
                        "SELECT ocekavany_odjezd, realny_prijezd FROM vlaky WHERE cislo = ? AND nazev = ? AND provozovatel = ? ORDER BY aktualizovano DESC LIMIT 1",
                        (train_no, train_name, carrier),
                    ).fetchall()
                    # train arrived, but we don't have its departure -> skipping
                    if len(last) != 1:
                        continue
                        logging.info(
                            "Prijezd bez evidovaneho odjezdu: %s %s (%s)",
                            train_no,
                            train_name,
                            carrier,
                        )
                        continue

                    # vlak uz mame dojety
                    if last[0][1]:
                        continue

                    ts = dt.datetime.fromisoformat(last[0][0])
                    if ts < dt.datetime.now(SZ_TZ) - dt.timedelta(hours=12):
                        logging.info(
                            "Vlak %s %s (%s) nejspis nepatri k nam do dat",
                            train_no,
                            train_name,
                            carrier,
                        )
                        continue
                    date = ts.date()
                    logging.info("Prijezd: %s %s (%s)", train_no, train_name, carrier)

                if latest_st == dep_st:
                    date = planned_time.date().isoformat()
                    arrival_planned, arrival_real = None, None
                    have = conn.execute(
                        "SELECT count(*) FROM vlaky WHERE datum_odjezd = ? AND cislo = ? AND nazev = ? AND provozovatel = ?",
                        (date, train_no, train_name, carrier),
                    ).fetchall()
                    # our departure is in the db already
                    if have[0][0] == 1:
                        continue

                    logging.info("Odjezd: %s %s (%s)", train_no, train_name, carrier)

                # TODO: asi by bylo cistsi ziskat si ID v tom prijezdu a podle nej udelat UPDATE
                # a v te druhe branch udelat jednoduchy INSERT
                conn.execute(
                    """INSERT INTO vlaky VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT DO UPDATE SET
                            aktualizovano=excluded.aktualizovano,
                            ocekavany_prijezd=excluded.ocekavany_prijezd,
                            realny_prijezd=excluded.realny_prijezd
                        """,
                    (
                        now.isoformat(),
                        now.isoformat(),
                        train_no,
                        train_name,
                        carrier,
                        date,
                        dep_st,
                        dest_st,
                        departure_planned.isoformat(),
                        departure_real.isoformat(),
                        arrival_planned.isoformat() if arrival_planned else None,
                        arrival_real.isoformat() if arrival_real else None,
                    ),
                )
                conn.commit()


# "type": "V", # assert?
# "a": 11.506427668344378, # podle me neco jako urazena vzdalenost
# "tt": "EC", # typ vlaku
# "tn": "332", # cislo vlaku
# "na": "Jižní expres", # nazev
# "fn": "Linz Hbf", # vychozi stanice
# "ln": "Praha hl.n.", # cilova stanice
# "cna": "Benešov u Prahy", # potvrzena stanice (nestavel tam nutne)
# "de": 3, # zpozdeni minut
# "nna": "Mrač z", # pristi stanice, kterou projede
# "r": "1154", # enum z par hodnot, tezko rict jakych
# "rr": 0, # enum 0/1, nevim ceho - podle me rr je dojezd (ale ne vzdy...)
# "d": "České dráhy, a.s.", # dopravce
# "s": 0, # vzdy nula
# "di": 0,
# "cp": "15:02", # planovany odjezd z `cna`
# "cr": "15:05", # realny odjezd z `cna`
# "pde": "3 min", # zpozdeni?
# "nsn": "Praha hl.n.", # pristi stanice, kde zastavi?
# "nst": "15:39", # pravidelny prijezd do pristi stanice
# "nsp": "15:42", # predpokladany prijezd do stanice
# "zst_sr70": "550665" # sr70 ID pro `nna`
