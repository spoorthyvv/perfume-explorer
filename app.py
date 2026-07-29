import os
import time
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import clickhouse_connect
from typing import Optional
from langfuse import get_client, observe, propagate_attributes

load_dotenv()

# Langfuse reads these env vars automatically:
# LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

app = FastAPI()
templates = Jinja2Templates(directory="templates")

client = clickhouse_connect.get_client(
    host=os.environ['CH_HOST'],
    port=8443,
    username=os.environ.get('CH_USER', 'default'),
    password=os.environ['CH_PASSWORD'],
    secure=True
)

langfuse = get_client()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]
    return templates.TemplateResponse(request=request, name="index.html", context={
        "brands": brands, "seasons": seasons,
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
    filters_input = {"q": q, "brand": brand, "gender": gender, "season": season,
                     "min_price": min_price, "max_price": max_price, "min_rating": min_rating}

    with langfuse.start_as_current_observation(
        as_type="span",
        name="perfume-search",
        input=filters_input,
    ) as root_span:
        with propagate_attributes(tags=["perfume-explorer"], metadata={"source": "web-ui"}):

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

            with langfuse.start_as_current_observation(
                as_type="span", name="clickhouse-query", input={"sql": query}
            ) as query_span:
                start = time.time()
                rows = client.query(query).result_rows
                duration_ms = round((time.time() - start) * 1000, 2)
                query_span.update(output={"row_count": len(rows), "duration_ms": duration_ms})

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
            with langfuse.start_as_current_observation(
                as_type="span", name="clickhouse-stats-query", input={"sql": stats_query}
            ) as stats_span:
                s = client.query(stats_query).result_rows[0]
                stats_span.update(output={"count": s[0], "avg_price": s[1], "avg_rating": s[2], "avg_longevity": s[3]})

            stats = {"count": s[0], "avg_price": s[1], "avg_rating": s[2], "avg_longevity": s[3]}
            root_span.update(output={"results_count": len(perfumes), "query_duration_ms": duration_ms})

    langfuse.flush()

    brands = [r[0] for r in client.query("SELECT DISTINCT brand FROM perfume_db.catalog ORDER BY brand").result_rows]
    seasons_list = [r[0] for r in client.query("SELECT DISTINCT season FROM perfume_db.catalog ORDER BY season").result_rows]

    return templates.TemplateResponse(request=request, name="index.html", context={
        "brands": brands, "seasons": seasons_list,
        "perfumes": perfumes, "stats": stats,
        "filters": filters_input
    })
