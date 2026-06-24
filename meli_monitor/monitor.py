#!/usr/bin/env python3
"""
Monitor de precios de MercadoLibre.

Modos:
    python monitor.py                  # tabla en consola (snapshot)
    python monitor.py --csv            # exporta snapshot a CSV
    python monitor.py --watch 60       # loop: detecta cambios cada 60s, imprime en consola
    python monitor.py --sheets         # one-shot: detecta cambios y los registra en Google Sheets

Variables de entorno:
    GOOGLE_CREDENTIALS_JSON   Contenido del JSON de la cuenta de servicio de Google
    SPREADSHEET_ID            ID del Google Sheet (el tramo largo de la URL)
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

MELI_API   = "https://api.mercadolibre.com/items"
BATCH_SIZE = 20
ITEMS_FILE = os.path.join(os.path.dirname(__file__), "items.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), ".last_prices.json")

SHEET_HEADERS = [
    "Fecha detección", "ID publicación", "Vendedor", "Producto",
    "Precio anterior", "Precio nuevo", "Diferencia $", "Diferencia %", "URL",
]


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
                    "title":          item.get("title", ""),
                    "price":          item.get("price"),
                    "original_price": item.get("original_price"),
                    "currency":       item.get("currency_id", "ARS"),
                    "status":         item.get("status", ""),
                    "permalink":      item.get("permalink", ""),
                }
    return results


# ── Estado persistente (para --sheets y reinicio de --watch) ─────────────────

def load_state() -> dict[str, float | None]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(prices: dict[str, float | None]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f)


# ── Formato ───────────────────────────────────────────────────────────────────

def fmt_price(amount, currency: str) -> str:
    if amount is None:
        return "-"
    symbol = "$" if currency == "ARS" else currency + " "
    return f"{symbol}{amount:,.2f}"


def build_rows(items: list[dict], prices: dict[str, dict]) -> list[dict]:
    rows = []
    for item in items:
        iid  = item["id"]
        data = prices.get(iid)
        if data is None:
            rows.append({
                "id": iid, "seller": item.get("seller", ""),
                "label": item.get("label", ""), "title": "ID no encontrado",
                "price": None, "original": None, "currency": "",
                "status": "error", "permalink": "",
            })
        else:
            rows.append({
                "id":       iid,
                "seller":   item.get("seller", ""),
                "label":    item.get("label", ""),
                "title":    data["title"],
                "price":    data["price"],
                "original": data["original_price"],
                "currency": data["currency"],
                "status":   data["status"],
                "permalink":data["permalink"],
            })
    return rows


def print_table(rows: list[dict]) -> None:
    headers = ["ID", "Vendedor", "Título", "Precio", "Precio original", "Estado"]
    col_w   = [14, 14, 45, 18, 18, 10]
    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"

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
        orig = fmt_price(r["original"], r["currency"]) if r["original"] else "-"
        print(row_fmt([r["id"], r["seller"], r["title"],
                       fmt_price(r["price"], r["currency"]), orig, r["status"]]))
    print(sep)


# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet():
    import gspread
    from google.oauth2.service_account import Credentials

    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("Falta la variable de entorno GOOGLE_CREDENTIALS_JSON")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError("Falta la variable de entorno SPREADSHEET_ID")

    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc     = gspread.authorize(creds)
    sh     = gc.open_by_key(spreadsheet_id)

    try:
        ws = sh.worksheet("Historial")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Historial", rows=1000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS)

    return ws


def append_changes_to_sheet(ws, changes: list[dict]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for c in changes:
        prev    = c["prev_price"]
        curr    = c["price"]
        diff    = (curr - prev) if (curr is not None and prev is not None) else None
        diff_pct = (diff / prev * 100) if (diff is not None and prev) else None
        rows.append([
            now,
            c["id"],
            c["seller"],
            c["label"] or c["title"],
            prev,
            curr,
            round(diff, 2)    if diff is not None else "",
            round(diff_pct, 2) if diff_pct is not None else "",
            c["permalink"],
        ])
    ws.append_rows(rows, value_input_option="USER_ENTERED")


# ── CSV export ────────────────────────────────────────────────────────────────

def export_csv(rows: list[dict]) -> str:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(os.path.dirname(__file__), f"prices_{ts}.csv")
    fields   = ["timestamp", "id", "label", "seller", "title",
                 "price", "original_price", "currency", "status", "permalink"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "timestamp": ts, "original_price": r["original"]})
    return filename


# ── Detección de cambios ──────────────────────────────────────────────────────

def detect_changes(rows: list[dict], last: dict[str, float | None]) -> list[dict]:
    changed = []
    for r in rows:
        iid  = r["id"]
        curr = r["price"]
        prev = last.get(iid, ...)
        if prev is ...:
            continue
        if curr != prev:
            changed.append({**r, "prev_price": prev})
    return changed


# ── Modos principales ─────────────────────────────────────────────────────────

def run_sheets(items: list[dict]) -> None:
    """One-shot: compara con precios anteriores y loguea cambios en Sheets."""
    ids    = [i["id"] for i in items]
    prices = fetch_prices(ids)
    rows   = build_rows(items, prices)
    last   = load_state()

    changes = detect_changes(rows, last)

    new_state = {r["id"]: r["price"] for r in rows}
    save_state(new_state)

    if not changes:
        print(f"[{datetime.now():%H:%M:%S}] Sin cambios de precio.")
        return

    print(f"[{datetime.now():%H:%M:%S}] {len(changes)} cambio(s) detectado(s). Registrando en Sheets...")
    ws = get_sheet()
    append_changes_to_sheet(ws, changes)

    for c in changes:
        arrow = "▼" if (c["price"] or 0) < (c["prev_price"] or 0) else "▲"
        print(f"  {arrow} {c['id']} | {c['seller']} | "
              f"{fmt_price(c['prev_price'], c['currency'])} → "
              f"{fmt_price(c['price'], c['currency'])}")

    print("Listo.")


def run_watch(items: list[dict], interval: int) -> None:
    """Loop continuo: imprime cambios en consola."""
    print(f"Monitoreando {len(items)} publicaciones cada {interval}s. Ctrl+C para salir.\n")
    last: dict[str, float | None] = {}

    while True:
        ids = [i["id"] for i in items]
        try:
            prices = fetch_prices(ids)
        except requests.RequestException as e:
            print(f"[{datetime.now():%H:%M:%S}] Error API: {e}")
            time.sleep(interval)
            continue

        rows    = build_rows(items, prices)
        changes = detect_changes(rows, last)

        for r in rows:
            last[r["id"]] = r["price"]

        ts = datetime.now().strftime("%H:%M:%S")
        if changes:
            print(f"\n[{ts}] CAMBIOS:")
            for c in changes:
                arrow = "▼" if (c["price"] or 0) < (c["prev_price"] or 0) else "▲"
                print(f"  {arrow} {c['id']} | {c['seller']} | {c['label'] or c['title'][:35]}")
                print(f"      {fmt_price(c['prev_price'], c['currency'])} → "
                      f"{fmt_price(c['price'], c['currency'])}")
                print(f"      {c['permalink']}")
        else:
            print(f"[{ts}] Sin cambios. Próxima revisión en {interval}s.")

        time.sleep(interval)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor de precios MercadoLibre")
    parser.add_argument("--items",  default=ITEMS_FILE)
    parser.add_argument("--csv",    action="store_true", help="Exportar snapshot a CSV")
    parser.add_argument("--watch",  type=int, metavar="SEGUNDOS",
                        help="Loop en consola cada N segundos")
    parser.add_argument("--sheets", action="store_true",
                        help="One-shot: detectar cambios y loguear en Google Sheets")
    args = parser.parse_args()

    if not os.path.exists(args.items):
        print(f"Error: no se encontró {args.items}")
        sys.exit(1)

    items = load_items(args.items)
    if not items:
        print("items.json está vacío.")
        sys.exit(1)

    if args.sheets:
        run_sheets(items)
        return

    if args.watch:
        run_watch(items, args.watch)
        return

    ids = [i["id"] for i in items]
    print(f"Consultando {len(ids)} publicaciones...\n")
    try:
        prices = fetch_prices(ids)
    except requests.RequestException as e:
        print(f"Error API: {e}")
        sys.exit(1)

    rows = build_rows(items, prices)
    print_table(rows)

    if args.csv:
        path = export_csv(rows)
        print(f"\nExportado a: {path}")


if __name__ == "__main__":
    main()
