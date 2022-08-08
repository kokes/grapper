import json
import logging
import time
import datetime as dt
from dataclasses import dataclass
from urllib.request import urlopen, Request
from urllib.parse import quote, unquote

import lxml.html


HTTP_TIMEOUT = 10
ALL_TRAINS_TICK_SECONDS = 10

URL_ALL_TRAINS = "https://grapp.spravazeleznic.cz/post/trains/GetTrainsWithFilter/1BDAEEBBC0E34FA8CBE8561A893B7ED4E99B63B0714E47E0DD49B1142B78E33D"
BODY_ALL_TRAINS = b'{"CarrierCode":["991919","992230","992719","993030","990010","993188","991943","991950","991075","993196","992693","991638","991976","993089","993162","991257","991935","991562","991125","992644","992842","991927","993170","991810","992909","991612","f_o_r_e_i_g_n"],"PublicKindOfTrain":["LE","Ex","Sp","rj","TL","EC","SC","AEx","Os","Rx","TLX","IC","EN","R","RJ","nj","LET"],"FreightKindOfTrain":[],"TrainRunning":false,"TrainNoChange":0,"TrainOutOfOrder":false,"Delay":["0","60","5","61","15","-1","30"],"DelayMin":-99999,"DelayMax":-99999,"SearchByTrainNumber":true,"SearchExtraTrain":false,"SearchByTrainName":true,"SearchByTRID":false,"SearchByVehicleNumber":false,"SearchTextType":"0","SearchPhrase":"","SelectedTrain":-1}'

URL_MAININFO = "https://grapp.spravazeleznic.cz/OneTrain/MainInfo/1BDAEEBBC0E34FA8CBE8561A893B7ED4E99B63B0714E47E0DD49B1142B78E33D?trainId={train_id}&_={ts}"
URL_ROUTEINFO = "https://grapp.spravazeleznic.cz/OneTrain/RouteInfo/1BDAEEBBC0E34FA8CBE8561A893B7ED4E99B63B0714E47E0DD49B1142B78E33D?trainId={train_id}&_={ts}"


@dataclass(frozen=True)
class Train:
    id: int
    name: str


@dataclass
class Station:
    name: str
    planned_departure: str  # time.time?
    actual_departure: str  # Optional?
    planned_arrival: str
    actual_arrival: str


@dataclass
class Route:
    train: Train
    carrier: str
    stations: list[Station]
    arrived: bool


def get_all_trains():
    req = Request(URL_ALL_TRAINS)
    req.add_header("content-type", "application/json; charset=UTF-8")
    req.data = BODY_ALL_TRAINS
    with urlopen(req, timeout=HTTP_TIMEOUT) as r:
        dt = json.load(r)

    return {Train(id=j["Id"], name=j["Title"].strip()) for j in dt["Trains"]}


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)  # TODO: time
    all_trains = set()
    while True:
        trains = get_all_trains()
        logging.info("načteno %d vlaků", len(trains))
        new_trains = trains - all_trains
        removed_trains = all_trains - trains
        if all_trains and new_trains:
            logging.info("%d nových vlaků", len(new_trains))
        if all_trains and removed_trains:
            logging.info("%d odebraných vlaků", len(removed_trains))

        all_trains = trains

        for train in all_trains:
            ts = int(dt.datetime.now().timestamp())
            url = URL_ROUTEINFO.format(train_id=train.id, ts=ts)
            with urlopen(url, timeout=HTTP_TIMEOUT) as r:
                dt = r.read().decode("utf-8")
                with open(URL_ROUTEINFO.split("/")[4] + ".html", "wt") as fw:
                    fw.write(dt)

                ht = lxml.html.fromstring(dt)

            carrier = ht.find(".//a[@class='carrierRestrictionLink']").text.strip()
            current_station = ht.find(".//a[@id='currentStation']").text_content().strip()

            rows = ht.find(".//div[@class='route']").findall("div[@class='row']")
            stations = []
            for row in rows:
                name = row.find("div").text_content().strip()
                actuals = [
                    j.text_content().strip()
                    for j in row.findall(".//span[@class='delayFuture_text']") + row.findall(".//span[@class='delayTo5_text']")
                ][:2] # obcas tam jsou ctyri, nevim proc
                if len(actuals) != 2:
                    breakpoint()
                assert len(actuals) == 2, actuals
                planned = [
                    j.text_content().strip().replace("(", "").replace(")", "")
                    for j in row.findall(".//span[@class='timeTT']")
                ][:2]
                assert len(planned) == 2, planned
                station = Station(
                    name=name,
                    actual_arrival=actuals[0],
                    actual_departure=actuals[1],
                    planned_arrival=planned[0],
                    planned_departure=planned[1],
                )
                stations.append(station)

            route = Route(
                train=train,
                carrier=carrier,
                stations=stations,
                arrived=current_station == stations[-1].name
            )
            breakpoint()

        time.sleep(ALL_TRAINS_TICK_SECONDS)
