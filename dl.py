import json
import logging
import time
import os
import datetime as dt
import random
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
WHERE dojel is TRUE
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
    id INT PRIMARY_KEY UNIQUE NOT NULL,
    nazev TEXT NOT NULL,
    provozovatel TEXT NOT NULL,
    stanice_vychozi TEXT NOT NULL,
    stanice_cilova TEXT NOT NULL,
    ocekavany_odjezd TIME NOT NULL,
    realny_odjezd TIME NOT NULL,
    ocekavany_prijezd TIME NOT NULL,
    realny_prijezd TIME NOT NULL,
    delka_cesty_minut INT NOT NULL, -- TODO: generated always as (stejne jako dalsi dva)
    zpozdeni_odjezd INT,
    zpozdeni_prijezd INT,
    dojel BOOL NOT NULL
)
"""


@dataclass(frozen=True, order=True)
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
    train: Train
    carrier: str
    stations: list[Station]
    planned_arrival: time
    expected_journey_minutes: int
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
    current_station_el = ht.find(".//*[@id='currentStation']")
    if current_station_el is None:
        return None  # vlastne nevim, kdy to nastane
    current_station = current_station_el.text_content().strip()

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
        planned_arrival=stations[-1].planned_arrival,
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

    all_routes = dict()
    cur = conn.execute(
        "SELECT id, nazev, ocekavany_prijezd, dojel FROM vlaky"
    ).fetchall()
    for tid, name, arrival, arrived in cur:
        train = Train(id=tid, name=name)
        all_routes[train] = Route(
            train=train,
            carrier=None,
            stations=None,
            planned_arrival=dt.time.fromisoformat(arrival),
            expected_journey_minutes=None,
            arrived=arrived,
        )

    logging.info("Načteno %d vlaků z disku", len(cur))

    while True:
        trains = get_all_trains(token)
        assert len(trains) > 0
        logging.info("načteno %d vlaků z API", len(trains))
        new_trains = trains - set(all_routes.keys())
        if all_routes and new_trains:
            logging.info("%d nových vlaků", len(new_trains))

        for new_train in new_trains:
            all_routes[new_train] = None

        for route in list(all_routes.keys()):
            if all_routes[route] and all_routes[route].arrived:
                del all_routes[route]

        # materializace, protoze budem menit slovnik
        randomised = list(all_routes.keys())
        random.shuffle(randomised)
        logging.info("Nahravám info o %d vlacích", len(randomised))
        for train in randomised:
            if all_routes.get(train):
                # logging.info("tenhle vlak (%s) jsme uz videli", train)
                arrival = dt.datetime.combine(
                    dt.date.today(), all_routes[train].planned_arrival
                )
                # logging.info("ocekavame ho v %s", arrival)
                if dt.datetime.now() < arrival - dt.timedelta(minutes=15):
                    # logging.info("Jeste ho nebudem nacitat, je moc brzo")
                    # abychom nemeli superrychlou loopu ve chvili, kdy uz mame nacachovano vsechno
                    time.sleep(1)
                    continue

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

            delay_departure, delay_arrival = None, None
            if route.arrived:
                delay_departure = delay_minutes(
                    route.stations[0].planned_departure,
                    route.stations[0].actual_departure,
                )
                delay_arrival = delay_minutes(
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
                    delay_arrival,
                )
                if train in all_routes:
                    del all_routes[train]
            conn.execute(
                """INSERT INTO vlaky VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET
                    zpozdeni_odjezd=excluded.zpozdeni_odjezd,
                    zpozdeni_prijezd=excluded.zpozdeni_prijezd,
                    dojel=excluded.dojel,
                    realny_odjezd=excluded.realny_odjezd,
                    realny_prijezd=excluded.realny_prijezd
                """,
                (
                    dt.date.today(),
                    train.id,
                    train.name,
                    route.carrier,
                    route.stations[0].name,
                    route.stations[-1].name,
                    route.stations[0].planned_departure.isoformat(),
                    route.stations[0].actual_departure.isoformat(),
                    route.stations[-1].planned_arrival.isoformat(),
                    route.stations[-1].actual_arrival.isoformat(),
                    route.expected_journey_minutes,
                    delay_departure,
                    delay_arrival,
                    route.arrived,
                ),
            )
            conn.commit()

            all_routes[train] = route

            time.sleep(1)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)  # TODO: time

    while True:
        try:
            with urlopen("https://grapp.spravazeleznic.cz", timeout=HTTP_TIMEOUT) as r:
                ht = lxml.html.parse(r)
                token = ht.find(".//input[@id='token']").value
                logging.info("mame token: %s", token)

            main(token)
        except socket.timeout:
            logging.info("timeout ¯\_(ツ)_/¯")
            time.sleep(15)
