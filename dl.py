import json
import logging
import time
import datetime as dt
from dataclasses import dataclass
from urllib.request import urlopen, Request
from typing import Optional

import lxml.html


HTTP_TIMEOUT = 10

APP_ID = "D9887D056D3E1E0593B5DAF1BC43807E79E943C166B97097CBF249FBB93F80AA" # TODO: tohle se bude menit
# <input type="hidden" id="token" value="D9887D056D3E1E0593B5DAF1BC43807E79E943C166B97097CBF249FBB93F80AA" />

URL_ALL_TRAINS = "https://grapp.spravazeleznic.cz/post/trains/GetTrainsWithFilter/{APP_ID}"
BODY_ALL_TRAINS = b'{"CarrierCode":["991919","992230","992719","993030","990010","993188","991943","991950","991075","993196","992693","991638","991976","993089","993162","991257","991935","991562","991125","992644","992842","991927","993170","991810","992909","991612","f_o_r_e_i_g_n"],"PublicKindOfTrain":["LE","Ex","Sp","rj","TL","EC","SC","AEx","Os","Rx","TLX","IC","EN","R","RJ","nj","LET"],"FreightKindOfTrain":[],"TrainRunning":false,"TrainNoChange":0,"TrainOutOfOrder":false,"Delay":["0","60","5","61","15","-1","30"],"DelayMin":-99999,"DelayMax":-99999,"SearchByTrainNumber":true,"SearchExtraTrain":false,"SearchByTrainName":true,"SearchByTRID":false,"SearchByVehicleNumber":false,"SearchTextType":"0","SearchPhrase":"","SelectedTrain":-1}'

URL_ROUTEINFO = "https://grapp.spravazeleznic.cz/OneTrain/RouteInfo/{APP_ID}?trainId={train_id}&_={ts}"


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


def get_all_trains():
    req = Request(URL_ALL_TRAINS.format(APP_ID=APP_ID))
    req.add_header("content-type", "application/json; charset=UTF-8")
    req.data = BODY_ALL_TRAINS
    with urlopen(req, timeout=HTTP_TIMEOUT) as r:
        dt = json.load(r)

    return {Train(id=j["Id"], name=j["Title"].strip()) for j in dt["Trains"]}


def parse_route_from_html(ht) -> Optional[Route]:
    if ht.find(".//div[@class='alertTitle']"):
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
        spans = [j.text_content().strip() for j in row.xpath(".//span[not(@id='currentStation')]")][:4]
        if len(spans) != 4:
            breakpoint()
        assert len(spans) == 4, spans
        spans = [dt.time.fromisoformat(j.replace("(", "").replace(")", "")) for j in spans]

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
        expected_journey_minutes=delay_minutes(stations[0].planned_departure, stations[-1].planned_arrival),
        arrived=current_station == stations[-1].name,
    )

# tady predpokladame, ze oba casy jsou ze stejneho dne
# (coz nebude platit vzdy)
def delay_minutes(planned, actual):
    today = dt.datetime.today()
    return (dt.datetime.combine(today, actual) - dt.datetime.combine(today, planned)).total_seconds() / 60


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)  # TODO: time
    all_routes = dict()
    arrived = set() # TODO: nacti odnekud z db
    while True:
        trains = get_all_trains()
        assert len(trains) > 0 # TODO: refreshuj token (ten ale muze expirovat i u tech vlaku samotnych)
        logging.info("načteno %d vlaků", len(trains))
        new_trains = trains - set(all_routes.keys()) - arrived
        removed_trains = set(all_routes.keys()) - trains - arrived
        if all_routes and new_trains:
            logging.info("%d nových vlaků", len(new_trains))
        if all_routes and removed_trains:
            logging.info("%d odebraných vlaků", len(removed_trains))

        for new_train in new_trains:
            all_routes[new_train] = None

        for train in all_routes.keys():
            ts = int(dt.datetime.now().timestamp())
            url = URL_ROUTEINFO.format(train_id=train.id, ts=ts, APP_ID=APP_ID)
            with urlopen(url, timeout=HTTP_TIMEOUT) as r:
                data = r.read().decode("utf-8")
                # TODO: smaz (jen pro introspekci)
                with open(URL_ROUTEINFO.split("/")[4] + ".html", "wt") as fw:
                    fw.write(data)

                ht = lxml.html.fromstring(data)

            route = parse_route_from_html(ht)
            if not route:
                logging.info("Info o vlaku %s uz neni", train.name)
                if train in all_routes:
                    del all_routes[train]
                continue
            if route.arrived:
                arrived.add(train)
                logging.info(
                    "Vlak %s (%s) dojel. [%s, %s]. Plánovaná jízda: %s, zpoždění (minut): %s",
                    train.name,
                    route.carrier,
                    route.stations[0].name, route.stations[-1].name,
                    route.expected_journey_minutes,
                    delay_minutes(route.stations[-1].planned_arrival, route.stations[-1].actual_arrival),
                )
                # TODO: zapisuj nekam
                if train in all_routes:
                    del all_routes[train]
            all_routes[train] = route

            time.sleep(1)
