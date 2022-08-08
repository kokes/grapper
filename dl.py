import json
import logging
import time
import os
import datetime as dt
from dataclasses import dataclass
from urllib.request import urlopen, Request
from typing import Optional
import socket
import sqlite3

import lxml.html

"""
-- zakladni analytika
SELECT
	provozovatel,
	count(*) pocet_jizd,
	round(avg(zpozdeni), 2) prumerne_zpozdeni,
	round(sum(case when zpozdeni <= 5 then 1 else 0 end)/cast(count(*) as float), 2) pod_5min,
	round(sum(case when zpozdeni <= 15 then 1 else 0 end)/cast(count(*) as float), 2) pod_15min
FROM
	vlaky
GROUP BY
	1
ORDER BY 2 desc
LIMIT 100
"""


HTTP_TIMEOUT = 10

URL_ALL_TRAINS = (
    "https://grapp.spravazeleznic.cz/post/trains/GetTrainsWithFilter/{APP_ID}"
)
BODY_ALL_TRAINS = b'{"CarrierCode":["991919","992230","992719","993030","990010","993188","991943","991950","991075","993196","992693","991638","991976","993089","993162","991257","991935","991562","991125","992644","992842","991927","993170","991810","992909","991612","f_o_r_e_i_g_n"],"PublicKindOfTrain":["LE","Ex","Sp","rj","TL","EC","SC","AEx","Os","Rx","TLX","IC","EN","R","RJ","nj","LET"],"FreightKindOfTrain":[],"TrainRunning":false,"TrainNoChange":0,"TrainOutOfOrder":false,"Delay":["0","60","5","61","15","-1","30"],"DelayMin":-99999,"DelayMax":-99999,"SearchByTrainNumber":true,"SearchExtraTrain":false,"SearchByTrainName":true,"SearchByTRID":false,"SearchByVehicleNumber":false,"SearchTextType":"0","SearchPhrase":"","SelectedTrain":-1}'

URL_ROUTEINFO = "https://grapp.spravazeleznic.cz/OneTrain/RouteInfo/{APP_ID}?trainId={train_id}&_={ts}"

SQLITE_TRAINS = """
CREATE TABLE vlaky (
    datum DATE NOT NULL,
    id INT NOT NULL, -- TODO: unique? pk?
    nazev TEXT NOT NULL,
    provozovatel TEXT NOT NULL,
    stanice_vychozi TEXT NOT NULL,
    stanice_cilova TEXT NOT NULL,
    delka_cesty FLOAT NOT NULL,
    zpozdeni FLOAT NOT NULL
)
"""


@dataclass(frozen=True)
class Train:
    id: int
    name: str


@dataclass
class Station:
    name: str
    planned_departure: dt.time
    actual_departure: dt.time
    planned_arrival: dt.time
    actual_arrival: dt.time


@dataclass
class Route:
    # last_checked: dt.datetime # TODO: nekontroluj routu kazdou minutu
    train: Train
    carrier: str
    stations: list[Station]
    expected_journey_minutes: float
    arrived: bool


def get_all_trains(token):
    req = Request(URL_ALL_TRAINS.format(APP_ID=token))
    req.add_header("content-type", "application/json; charset=UTF-8")
    req.data = BODY_ALL_TRAINS
    with urlopen(req, timeout=HTTP_TIMEOUT) as r:
        dt = json.load(r)

    return {Train(id=j["Id"], name=j["Title"].strip()) for j in dt["Trains"]}


def parse_route_from_html(ht, train) -> Optional[Route]:
    if ht.find(".//div[@class='alertTitle']") is not None:
        return None
    carrier = ht.find(".//a[@class='carrierRestrictionLink']").text.strip()
    # nekdy je to odkaz, nekdy span
    current_station = ht.find(".//*[@id='currentStation']").text_content().strip()

    rows = ht.find(".//div[@class='route']").findall("div[@class='row']")
    stations = []
    for row in rows:
        name = row.find("div").text_content().strip()
        # obcas nejsou ctyri, nevim uplne proc
        # a prvni span je obcas soucasna stanice, tak tu musime vyradit
        spans = [
            j.text_content().strip()
            for j in row.xpath(".//span[not(@id='currentStation')]")
        ][:4]
        if len(spans) != 4:
            breakpoint()
        assert len(spans) == 4, spans
        spans = [
            dt.time.fromisoformat(j.replace("(", "").replace(")", "")) for j in spans
        ]

        station = Station(
            name=name,
            actual_arrival=spans[0],
            planned_arrival=spans[1],
            actual_departure=spans[2],
            planned_departure=spans[3],
        )
        stations.append(station)

    assert len(stations) > 0

    return Route(
        train=train,
        carrier=carrier,
        stations=stations,
        expected_journey_minutes=delay_minutes(
            stations[0].planned_departure, stations[-1].planned_arrival
        ),
        arrived=current_station == stations[-1].name,
    )


# tady predpokladame, ze oba casy jsou ze stejneho dne
# (coz nebude platit vzdy)
def delay_minutes(planned, actual):
    today = dt.datetime.today()
    return (
        dt.datetime.combine(today, actual) - dt.datetime.combine(today, planned)
    ).total_seconds() / 60


def main(token: str):
    dbfile = "vlaky.db"
    dbexists = os.path.isfile(dbfile)
    conn = sqlite3.connect(dbfile)
    if not dbexists:
        conn.execute(SQLITE_TRAINS)

    arrived = set()
    cur = conn.execute("SELECT id, nazev FROM vlaky").fetchall()
    for tid, name in cur:
        arrived.add(Train(id=tid, name=name))
    logging.info("Načteno %d dorazivších vlaků", len(cur))

    all_routes = dict()
    while True:
        trains = get_all_trains(token)
        assert (
            len(trains) > 0
        )  # TODO: refreshuj token (ten ale muze expirovat i u tech vlaku samotnych)
        logging.info(
            "načteno %d vlaků (%d po odečtení již zapsaných)",
            len(trains),
            len(trains - arrived),
        )
        new_trains = trains - set(all_routes.keys()) - arrived
        removed_trains = set(all_routes.keys()) - trains - arrived
        if all_routes and new_trains:
            logging.info("%d nových vlaků", len(new_trains))
        if all_routes and removed_trains:
            logging.info("%d odebraných vlaků", len(removed_trains))

        for new_train in new_trains:
            all_routes[new_train] = None

        # materializace, protoze budem menit slovnik
        for train in list(all_routes.keys()):
            ts = int(dt.datetime.now().timestamp())
            url = URL_ROUTEINFO.format(train_id=train.id, ts=ts, APP_ID=token)
            with urlopen(url, timeout=HTTP_TIMEOUT) as r:
                data = r.read().decode("utf-8")
                # TODO: smaz (jen pro introspekci)
                with open(URL_ROUTEINFO.split("/")[4] + ".html", "wt") as fw:
                    fw.write(data)

                ht = lxml.html.fromstring(data)

            route = parse_route_from_html(ht, train)
            if not route:
                logging.info("Info o vlaku %s uz neni", train.name)
                if train in all_routes:
                    del all_routes[train]
                continue
            if route.arrived:
                arrived.add(train)
                delay = delay_minutes(
                    route.stations[-1].planned_arrival,
                    route.stations[-1].actual_arrival,
                )
                logging.info(
                    "Vlak %s (%s) dojel. [%s, %s]. Plánovaná jízda: %s, zpoždění (minut): %s",
                    train.name,
                    route.carrier,
                    route.stations[0].name,
                    route.stations[-1].name,
                    route.expected_journey_minutes,
                    delay,
                )
                conn.execute(
                    "INSERT INTO vlaky VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dt.date.today(),
                        train.id,
                        train.name,
                        route.carrier,
                        route.stations[0].name,
                        route.stations[-1].name,
                        route.expected_journey_minutes,
                        delay,
                    ),
                )
                conn.commit()
                if train in all_routes:
                    del all_routes[train]
            all_routes[train] = route

            time.sleep(1)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)  # TODO: time

    with urlopen("https://grapp.spravazeleznic.cz", timeout=HTTP_TIMEOUT) as r:
        ht = lxml.html.parse(r)
        token = ht.find(".//input[@id='token']").value
        logging.info("mame token: %s", token)

    while True:
        try:
            main(token)
        except socket.timeout:
            logging.info("timeout ¯\_(ツ)_/¯")
            time.sleep(15)
