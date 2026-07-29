import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import clickhouse_connect
from typing import Optional

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

client = clickhouse_connect.get_client(
    host=os.environ['CH_HOST'],
    port=8443,
    username=os.environ.get('CH_USER', 'default'),
    password=os.environ['CH_PASSWORD'],
    secure=True
)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]
    return templates.TemplateResponse("index.html", {
        "request": request, "brands": brands, "seasons": seasons,
        "perfumes": [], "stats": None, "filters": {}
    })

@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: Optional[str] = "",
    brand: Optional[str] = "",
    gender: Optional[str] = "",
    season: Optional[str] = "",
    min_price: Optional[float] = 0,
    max_price: Optional[float] = 500,
    min_rating: Optional[float] = 0,
):
    conditions = [
        f"price_usd BETWEEN {min_price} AND {max_price}",
        f"rating >= {min_rating}",
    ]
    if brand:
        conditions.append(f"brand = '{brand}'")
    if gender:
        conditions.append(f"gender = '{gender}'")
    if season:
        conditions.append(f"season = '{season}'")
    if q:
        safe_q = q.replace("'", "\\'")
        conditions.append(
            f"(name ILIKE '%{safe_q}%' OR brand ILIKE '%{safe_q}%' "
            f"OR arrayExists(x -> x ILIKE '%{safe_q}%', arrayConcat(top_notes, heart_notes, base_notes)))"
        )

    where = " AND ".join(conditions)
    query = f"""
        SELECT name, brand, category, gender,
               arrayStringConcat(top_notes, ', ') AS top,
               arrayStringConcat(heart_notes, ', ') AS heart,
               arrayStringConcat(base_notes, ', ') AS base,
               price_usd, rating, launch_year, longevity_hours, season
        FROM perfume_db.catalog
        WHERE {where}
        ORDER BY rating DESC
    """
    rows = client.query(query).result_rows

    perfumes = [
        {"name": r[0], "brand": r[1], "category": r[2], "gender": r[3],
         "top": r[4], "heart": r[5], "base": r[6], "price": r[7],
         "rating": r[8], "year": r[9], "longevity": r[10], "season": r[11]}
        for r in rows
    ]

    stats_query = f"""
        SELECT count(), round(avg(price_usd),0), round(avg(rating),1), round(avg(longevity_hours),0)
        FROM perfume_db.catalog WHERE {where}
    """
    s = client.query(stats_query).result_rows[0]
    stats = {"count": s[0], "avg_price": s[1], "avg_rating": s[2], "avg_longevity": s[3]}

    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons_list = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]

    return templates.TemplateResponse("index.html", {
        "request": request, "brands": brands, "seasons": seasons_list,
        "perfumes": perfumes, "stats": stats,
        "filters": {"q": q, "brand": brand, "gender": gender, "season": season,
                     "min_price": min_price, "max_price": max_price, "min_rating": min_rating}
    })
