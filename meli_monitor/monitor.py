#!/usr/bin/env python3
"""
Monitor de precios de MercadoLibre.
Lee IDs de publicaciones desde items.json y muestra/guarda los precios actuales.

Uso:
    python monitor.py               # muestra tabla en consola
    python monitor.py --csv         # exporta a prices_YYYYMMDD_HHMMSS.csv
    python monitor.py --watch 60    # revisa cada 60 segundos y alerta cambios

Variables de entorno para Telegram (opcional):
    TELEGRAM_TOKEN   Token del bot (de @BotFather)
    TELEGRAM_CHAT_ID ID del chat donde mandar alertas
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

MELI_API = "https://api.mercadolibre.com/items"
BATCH_SIZE = 20  # ML permite hasta 20 IDs por request
ITEMS_FILE = os.path.join(os.path.dirname(__file__), "items.json")


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_token() -> str | None:
    return os.environ.get("TELEGRAM_TOKEN")

def _tg_chat() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram(text: str) -> None:
    token = _tg_token()
    chat  = _tg_chat()
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException:
        pass  # no interrumpir el monitor si Telegram falla


# ── API MercadoLibre ──────────────────────────────────────────────────────────

def load_items(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_prices(ids: list[str]) -> dict[str, dict]:
    results = {}
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i : i + BATCH_SIZE]
        resp = requests.get(MELI_API, params={"ids": ",".join(batch)}, timeout=15)
        resp.raise_for_status()
        for entry in resp.json():
            if entry.get("code") == 200:
                item = entry["body"]
                results[item["id"]] = {
                    "title": item.get("title", ""),
                    "price": item.get("price"),
                    "original_price": item.get("original_price"),
                    "currency": item.get("currency_id", "ARS"),
                    "condition": item.get("condition", ""),
                    "status": item.get("status", ""),
                    "permalink": item.get("permalink", ""),
                    "seller_id": item.get("seller_id"),
                }
            else:
                results[batch[entry.get("code", 0) % len(batch)]] = None
    return results


# ── Formato ───────────────────────────────────────────────────────────────────

def format_price(amount, currency: str) -> str:
    if amount is None:
        return "-"
    symbol = "$" if currency == "ARS" else currency + " "
    return f"{symbol}{amount:,.2f}"


def discount_pct(price, original) -> str:
    if not original or not price or original <= price:
        return ""
    pct = (1 - price / original) * 100
    return f"  -{pct:.0f}%"


def print_table(rows: list[dict]) -> None:
    headers = ["ID", "Vendedor", "Título", "Precio", "Precio original", "Estado", "URL"]
    col_w = [14, 14, 40, 18, 18, 10, 50]

    sep = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    def row_fmt(cells):
        parts = []
        for cell, w in zip(cells, col_w):
            cell = str(cell or "")
            parts.append(f" {cell[:w]:<{w}} ")
        return "|" + "|".join(parts) + "|"

    print(sep)
    print(row_fmt(headers))
    print(sep)
    for r in rows:
        print(row_fmt([
            r["id"],
            r["seller"],
            r["title"],
            r["price_fmt"] + r["discount"],
            r["original_fmt"],
            r["status"],
            r["permalink"],
        ]))
    print(sep)


def build_rows(items: list[dict], prices: dict[str, dict]) -> list[dict]:
    rows = []
    for item in items:
        iid = item["id"]
        data = prices.get(iid)
        if data is None:
            rows.append({
                "id": iid,
                "seller": item.get("seller", ""),
                "label": item.get("label", ""),
                "title": "ID no encontrado",
                "price": None,
                "original": None,
                "price_fmt": "-",
                "original_fmt": "-",
                "discount": "",
                "status": "error",
                "permalink": "",
                "currency": "",
            })
        else:
            rows.append({
                "id": iid,
                "seller": item.get("seller", ""),
                "label": item.get("label", ""),
                "title": data["title"],
                "price": data["price"],
                "original": data["original_price"],
                "price_fmt": format_price(data["price"], data["currency"]),
                "original_fmt": format_price(data["original_price"], data["currency"]),
                "discount": discount_pct(data["price"], data["original_price"]),
                "status": data["status"],
                "permalink": data["permalink"],
                "currency": data["currency"],
            })
    return rows


def export_csv(rows: list[dict]) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(os.path.dirname(__file__), f"prices_{ts}.csv")
    fieldnames = ["timestamp", "id", "label", "seller", "title", "price", "original_price",
                  "currency", "discount_pct", "status", "permalink"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            disc = ""
            if r["original"] and r["price"] and r["original"] > r["price"]:
                disc = f"{(1 - r['price'] / r['original']) * 100:.1f}"
            writer.writerow({
                "timestamp": ts,
                "id": r["id"],
                "label": r["label"],
                "seller": r["seller"],
                "title": r["title"],
                "price": r["price"],
                "original_price": r["original"],
                "currency": r["currency"],
                "discount_pct": disc,
                "status": r["status"],
                "permalink": r["permalink"],
            })
    return filename


# ── Watch mode ────────────────────────────────────────────────────────────────

def watch(items: list[dict], interval: int) -> None:
    tg_active = bool(_tg_token() and _tg_chat())
    print(f"Monitoreando {len(items)} publicaciones cada {interval}s.")
    if tg_active:
        print("Alertas Telegram: activadas")
        send_telegram(
            f"<b>Monitor MeLi iniciado</b>\n"
            f"Vigilando {len(items)} publicaciones cada {interval}s."
        )
    print("Ctrl+C para salir.\n")

    last_prices: dict[str, float | None] = {}

    while True:
        ids = [i["id"] for i in items]
        try:
            prices = fetch_prices(ids)
        except requests.RequestException as e:
            print(f"[{datetime.now():%H:%M:%S}] Error al consultar API: {e}")
            time.sleep(interval)
            continue

        rows = build_rows(items, prices)
        changed = []
        for r in rows:
            prev = last_prices.get(r["id"], ...)
            curr = r["price"]
            if prev is ...:
                last_prices[r["id"]] = curr
                continue
            if curr != prev:
                changed.append((r, prev))
                last_prices[r["id"]] = curr

        ts = datetime.now().strftime("%H:%M:%S")
        if changed:
            print(f"\n{'='*60}")
            print(f"[{ts}]  CAMBIOS DETECTADOS")
            print(f"{'='*60}")
            tg_lines = ["<b>Cambio de precio en MeLi</b>"]
            for r, prev in changed:
                prev_fmt = format_price(prev, r["currency"])
                curr_fmt = r["price_fmt"]
                arrow = "▼" if (r["price"] or 0) < (prev or 0) else "▲"
                print(f"  {arrow} {r['id']}  {r['title'][:40]}")
                print(f"     Vendedor: {r['seller']}")
                print(f"     Precio:   {prev_fmt}  →  {curr_fmt}")
                print(f"     URL:      {r['permalink']}")
                tg_lines.append(
                    f"\n{arrow} <b>{r['title'][:50]}</b>\n"
                    f"Vendedor: {r['seller']}\n"
                    f"Precio: {prev_fmt} → <b>{curr_fmt}</b>\n"
                    f"<a href=\"{r['permalink']}\">Ver publicación</a>"
                )
            if tg_active:
                send_telegram("\n".join(tg_lines))
        else:
            print(f"[{ts}]  Sin cambios. Próxima revisión en {interval}s.")

        time.sleep(interval)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor de precios MercadoLibre")
    parser.add_argument("--items", default=ITEMS_FILE, help="Path al JSON con las publicaciones")
    parser.add_argument("--csv", action="store_true", help="Exportar resultados a CSV")
    parser.add_argument("--watch", type=int, metavar="SEGUNDOS",
                        help="Monitorear en loop cada N segundos y alertar cambios")
    args = parser.parse_args()

    if not os.path.exists(args.items):
        print(f"Error: no se encontró el archivo de items: {args.items}")
        print("Creá un items.json con el formato del ejemplo.")
        sys.exit(1)

    items = load_items(args.items)
    if not items:
        print("El archivo items.json está vacío.")
        sys.exit(1)

    if args.watch:
        watch(items, args.watch)
        return

    ids = [i["id"] for i in items]
    print(f"Consultando {len(ids)} publicaciones...\n")
    try:
        prices = fetch_prices(ids)
    except requests.RequestException as e:
        print(f"Error al consultar la API de MercadoLibre: {e}")
        sys.exit(1)

    rows = build_rows(items, prices)
    print_table(rows)

    if args.csv:
        path = export_csv(rows)
        print(f"\nExportado a: {path}")


if __name__ == "__main__":
    main()
