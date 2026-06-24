#!/usr/bin/env python3
"""
Monitor de precios de MercadoLibre.

Uso:
    python monitor.py              # muestra precios actuales en consola
    python monitor.py --watch 300  # revisa cada 5 min y guarda cambios en historial.csv
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

import requests

MELI_API    = "https://api.mercadolibre.com/items"
BATCH_SIZE  = 20
ITEMS_FILE  = os.path.join(os.path.dirname(__file__), "items.json")
HISTORY_CSV = os.path.join(os.path.dirname(__file__), "historial.csv")
STATE_FILE  = os.path.join(os.path.dirname(__file__), ".last_prices.json")

CSV_HEADERS = [
    "Fecha", "ID", "Vendedor", "Producto",
    "Precio anterior", "Precio nuevo", "Diferencia $", "Diferencia %", "URL",
]


def load_items(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_prices(ids: list[str]) -> dict[str, dict]:
    results = {}
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i : i + BATCH_SIZE]
        resp  = requests.get(MELI_API, params={"ids": ",".join(batch)}, timeout=15)
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get("code") == 200:
                item = entry["body"]
                results[item["id"]] = {
                    "title":    item.get("title", ""),
                    "price":    item.get("price"),
                    "currency": item.get("currency_id", "ARS"),
                    "status":   item.get("status", ""),
                    "permalink":item.get("permalink", ""),
                }
    return results


def fmt(amount, currency="ARS") -> str:
    if amount is None:
        return "-"
    return f"{'$' if currency == 'ARS' else currency + ' '}{amount:,.2f}"


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def append_to_csv(changes: list[dict]) -> None:
    is_new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(CSV_HEADERS)
        for c in changes:
            prev = c["prev"]
            curr = c["price"]
            diff     = round(curr - prev, 2) if curr and prev else ""
            diff_pct = round((curr - prev) / prev * 100, 2) if curr and prev else ""
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                c["id"],
                c["seller"],
                c["label"] or c["title"],
                prev,
                curr,
                diff,
                diff_pct,
                c["permalink"],
            ])


def print_snapshot(items: list[dict], prices: dict) -> None:
    print(f"\n{'ID':<16} {'Vendedor':<15} {'Producto':<40} {'Precio':>14}  Estado")
    print("-" * 95)
    for item in items:
        data = prices.get(item["id"])
        if not data:
            print(f"{item['id']:<16} {'':15} {'ID no encontrado':<40}")
            continue
        nombre = item.get("label") or data["title"]
        print(f"{item['id']:<16} {item.get('seller',''):<15} {nombre[:40]:<40} "
              f"{fmt(data['price'], data['currency']):>14}  {data['status']}")
    print()


def watch(items: list[dict], interval: int) -> None:
    print(f"Monitoreando {len(items)} publicaciones cada {interval}s.")
    print(f"Los cambios se guardan en: {HISTORY_CSV}")
    print("Ctrl+C para salir.\n")

    last = load_state()

    while True:
        ids = [i["id"] for i in items]
        try:
            prices = fetch_prices(ids)
        except requests.RequestException as e:
            print(f"[{datetime.now():%H:%M:%S}] Error API: {e}")
            time.sleep(interval)
            continue

        changes = []
        new_state = dict(last)
        for item in items:
            iid  = item["id"]
            data = prices.get(iid)
            curr = data["price"] if data else None
            prev = last.get(iid, ...)
            new_state[iid] = curr
            if prev is ...:
                continue
            if curr != prev:
                changes.append({
                    "id":       iid,
                    "seller":   item.get("seller", ""),
                    "label":    item.get("label", ""),
                    "title":    data["title"] if data else "",
                    "price":    curr,
                    "prev":     prev,
                    "currency": data["currency"] if data else "ARS",
                    "permalink":data["permalink"] if data else "",
                })

        last = new_state
        save_state(new_state)

        ts = datetime.now().strftime("%H:%M:%S")
        if changes:
            append_to_csv(changes)
            print(f"[{ts}] {len(changes)} cambio(s) → guardado en historial.csv")
            for c in changes:
                arrow = "▼" if (c["price"] or 0) < (c["prev"] or 0) else "▲"
                print(f"  {arrow} {c['id']} | {c['seller']} | "
                      f"{fmt(c['prev'], c['currency'])} → {fmt(c['price'], c['currency'])}")
        else:
            print(f"[{ts}] Sin cambios. Próxima revisión en {interval}s.")

        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor de precios MercadoLibre")
    parser.add_argument("--items", default=ITEMS_FILE)
    parser.add_argument("--watch", type=int, metavar="SEGUNDOS",
                        help="Monitorear en loop y guardar cambios en historial.csv")
    args = parser.parse_args()

    if not os.path.exists(args.items):
        print(f"Error: no se encontró {args.items}")
        sys.exit(1)

    items = load_items(args.items)
    if not items:
        print("items.json está vacío.")
        sys.exit(1)

    ids = [i["id"] for i in items]
    try:
        prices = fetch_prices(ids)
    except requests.RequestException as e:
        print(f"Error API: {e}")
        sys.exit(1)

    print_snapshot(items, prices)

    if args.watch:
        watch(items, args.watch)


if __name__ == "__main__":
    main()
