from __future__ import annotations

import csv
import io
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from inventory_app.connectors.fassto import (
    FasstoApiError,
    FasstoConnector,
    build_warehousing_payload,
)
from inventory_app.services.card_api_client import CardApiClient
from inventory_app.services.card_category import DEFAULT_CATEGORIES
from inventory_app.services.channel_services import CoupangChannelService, NaverChannelService
from inventory_app.services.keyword_services import NaverKeywordRevenueService
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.purchase_history_service import (
    PurchaseHistoryParser,
    PurchaseHistoryStore,
)
from inventory_app.services.pi_data_client import PiDataClient
from inventory_app.services.revenue_services import RevenueComparisonService

from .config import config_status, load_web_config
from .jobs import jobs
from .serializers import channel_product_to_dict, monitor_inventory_row, to_jsonable

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))


def ok(data: Any = None, **extra: Any) -> JSONResponse:
    payload = {"ok": True, "data": to_jsonable(data)}
    payload.update(extra)
    return JSONResponse(payload)


def fail(message: str, status_code: int = 400, **extra: Any) -> JSONResponse:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status_code)


def _csv_response(rows: Iterable[dict[str, Any]], filename: str) -> StreamingResponse:
    rows_list = list(rows)
    output = io.StringIO()
    if rows_list:
        fieldnames = sorted({key for row in rows_list for key in row.keys()})
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_list)
    else:
        output.write("")
    body = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    return StreamingResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "rows", "records", "orders", "masters", "links", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _search_filter(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return rows
    return [
        row
        for row in rows
        if q in str(row.get("name") or row.get("title") or row.get("store_name") or row).lower()
    ]


def create_app() -> FastAPI:
    app = FastAPI(title="SmartInventory Web", version="0.1.0")
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    config = load_web_config()
    is_vercel = bool(os.environ.get("VERCEL"))

    def monitor_url() -> str:
        url = (config.monitor_url or "").strip().rstrip("/")
        if not url:
            raise HTTPException(
                status_code=424,
                detail=(
                    "Vercel deployment needs SMARTINVENTORY_MONITOR_URL. "
                    "Fixed-IP API calls must run on the Raspberry Pi backend."
                ),
            )
        return url

    def require_monitor_for_vercel(feature: str) -> None:
        if is_vercel and not config.monitor_url:
            raise HTTPException(
                status_code=424,
                detail=(
                    f"{feature} requires SMARTINVENTORY_MONITOR_URL on Vercel. "
                    "Fixed-IP API calls must run on the Raspberry Pi backend."
                ),
            )

    def monitor_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        base = monitor_url()
        try:
            resp = httpx.request(
                method,
                f"{base}{path}",
                params=params,
                json=json_body,
                timeout=timeout or max(10.0, float(config.timeout_seconds)),
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"backend request failed: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = {"body": resp.text}
        if resp.status_code >= 400:
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise HTTPException(status_code=resp.status_code, detail=detail or payload)
        return payload if isinstance(payload, dict) else {"data": payload}

    def make_fassto() -> FasstoConnector:
        return FasstoConnector(
            api_cd=config.fassto_api_cd,
            api_key=config.fassto_api_key,
            cst_cd=config.fassto_cst_cd,
            api_url=config.fassto_api_url,
            timeout_seconds=config.timeout_seconds,
        )

    def purchase_store() -> PurchaseHistoryStore:
        pi_client = None
        if config.monitor_url:
            pi_client = PiDataClient(
                config.monitor_url,
                timeout=max(10.0, float(config.timeout_seconds)),
                gist_raw_url=config.monitor_url_gist,
            )
        return PurchaseHistoryStore(pi_client=pi_client)

    def start_job(name: str, work: Callable[[Callable[[str], None], Callable[[int], None]], Any]) -> JSONResponse:
        job = jobs.create(name, work)
        return ok(job.snapshot())

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return fail(str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        return fail(str(exc), status_code=500)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "request": request,
                "tabs": [
                    ("masters", "상품관리"),
                    ("naver", "네이버"),
                    ("coupang", "쿠팡"),
                    ("sales", "판매일보"),
                    ("revenue", "매출비교"),
                    ("keywords", "키워드매출"),
                    ("purchases", "구매내역"),
                    ("cards", "카드사용내역"),
                    ("fassto", "파스토"),
                ],
            },
        )

    @app.get("/api/health")
    def health() -> JSONResponse:
        return ok(
            {
                "status": "ok",
                "time": datetime.now().isoformat(),
                "config": config_status(config),
            }
        )

    @app.get("/api/config/status")
    def get_config_status() -> JSONResponse:
        return ok(config_status(config))

    @app.get("/api/jobs")
    def list_jobs() -> JSONResponse:
        return ok([job.snapshot() for job in jobs.list()])

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            return fail("job not found", status_code=404)
        return ok(job.snapshot())

    @app.get("/api/channels/{channel}")
    def get_channel(
        channel: str,
        q: str = "",
        source: str = Query("auto", pattern="^(auto|live|cache)$"),
    ) -> JSONResponse:
        if channel not in {"naver", "coupang"}:
            return fail("unknown channel", status_code=404)
        require_monitor_for_vercel(f"{channel} inventory")
        warnings: list[str] = []
        rows: list[dict[str, Any]]
        if config.monitor_url and source != "cache":
            payload = monitor_request("GET", "/inventory")
            raw_rows = _as_list(payload.get(channel, []))
            rows = [
                monitor_inventory_row(channel, row, index)
                for index, row in enumerate(raw_rows, start=1)
                if isinstance(row, dict)
            ]
            warnings.append("monitor")
        else:
            service = NaverChannelService(config) if channel == "naver" else CoupangChannelService(config)
            if source == "live":
                service_rows, service_warnings = service.fetch()
            else:
                service_rows, service_warnings = service.fetch_cached()
            rows = [channel_product_to_dict(row) for row in service_rows]
            warnings.extend([str(w) for w in service_warnings])
        return ok({"rows": _search_filter(rows, q), "warnings": warnings})

    @app.post("/api/channels/{channel}/sync")
    def sync_channel(channel: str) -> JSONResponse:
        if channel not in {"naver", "coupang"}:
            return fail("unknown channel", status_code=404)
        require_monitor_for_vercel(f"{channel} sync")

        def work(log: Callable[[str], None], progress: Callable[[int], None]) -> dict[str, Any]:
            log(f"{channel} sync started")
            if config.monitor_url:
                progress(20)
                payload = monitor_request("POST", "/sync/inventory", params={"wait": "1"}, timeout=90.0)
                progress(80)
                return payload
            service = NaverChannelService(config) if channel == "naver" else CoupangChannelService(config)
            rows, warnings = service.fetch()
            progress(90)
            return {"rows": len(rows), "warnings": warnings}

        return start_job(f"{channel}-sync", work)

    @app.patch("/api/channels/{channel}/products/{product_key:path}")
    async def update_channel_product(channel: str, product_key: str, request: Request) -> JSONResponse:
        if channel not in {"naver", "coupang"}:
            return fail("unknown channel", status_code=404)
        body = await request.json()
        cache = ChannelProductCache()
        if "favorite" in body:
            cache.save_favorite(channel, product_key, bool(body.get("favorite")))
        if "customName" in body:
            cache.save_name_override(channel, product_key, body.get("customName"))
        if "unitCost" in body:
            raw_cost = body.get("unitCost")
            cache.save_cost_override(channel, product_key, int(raw_cost) if raw_cost not in (None, "") else None)
        return ok({"productKey": product_key})

    @app.get("/api/masters")
    def get_masters(include_links: bool = True) -> JSONResponse:
        if config.monitor_url:
            data = monitor_request("GET", "/masters")
            if include_links:
                data["links"] = monitor_request("GET", "/master-links").get("links", [])
            return ok(data)
        cache = ChannelProductCache()
        data = {"masters": cache.list_masters()}
        if include_links:
            data["links"] = list(cache.load_all_links().values())
        return ok(data)

    @app.post("/api/masters")
    async def create_master(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/masters", json_body=body))
        cache = ChannelProductCache()
        return ok({"master": cache.create_master(body.get("name"), body.get("unit_cost"), body.get("memo"))})

    @app.get("/api/masters/{master_id}")
    def get_master(master_id: int) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", f"/masters/{master_id}"))
        cache = ChannelProductCache()
        master = cache.get_master(master_id)
        if master is None:
            return fail("master not found", status_code=404)
        return ok({"master": master, "links": cache.list_links_for_master(master_id)})

    @app.patch("/api/masters/{master_id}")
    async def update_master(master_id: int, request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("PATCH", f"/masters/{master_id}", json_body=body))
        cache = ChannelProductCache()
        cache.update_master(master_id, **body)
        return ok({"master": cache.get_master(master_id)})

    @app.delete("/api/masters/{master_id}")
    def delete_master(master_id: int) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("DELETE", f"/masters/{master_id}"))
        ChannelProductCache().delete_master(master_id)
        return ok({"deleted": True})

    @app.put("/api/masters/{master_id}/representative")
    async def set_master_representative(master_id: int, request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("PUT", f"/masters/{master_id}/representative", json_body=body))
        ChannelProductCache().set_master_representative(master_id, body.get("channel"), body.get("product_key"))
        return ok({"masterId": master_id})

    @app.post("/api/master-links")
    async def link_master(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/master-links", json_body=body))
        ChannelProductCache().link_channel_product(
            body.get("channel"),
            body.get("product_key"),
            int(body.get("master_id")),
            int(body.get("multiplier") or 1),
        )
        return ok({"linked": True})

    @app.delete("/api/master-links")
    def unlink_master(channel: str, product_key: str) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("DELETE", "/master-links", params={"channel": channel, "product_key": product_key}))
        ChannelProductCache().unlink_channel_product(channel, product_key)
        return ok({"unlinked": True})

    @app.put("/api/master-links/multiplier")
    async def set_link_multiplier(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("PUT", "/master-links/multiplier", json_body=body))
        ChannelProductCache().set_link_multiplier(
            body.get("channel"),
            body.get("product_key"),
            int(body.get("multiplier") or 1),
        )
        return ok({"updated": True})

    @app.get("/api/stock-inbounds")
    def get_stock_inbounds(channel: str | None = None, master_id: int | None = None) -> JSONResponse:
        return ok(monitor_request("GET", "/stock-inbounds", params={"channel": channel, "master_id": master_id}))

    @app.post("/api/stock-inbounds")
    async def add_stock_inbound(request: Request) -> JSONResponse:
        return ok(monitor_request("POST", "/stock-inbounds", json_body=await request.json()))

    @app.delete("/api/stock-inbounds/{item_id}")
    def delete_stock_inbound(item_id: int) -> JSONResponse:
        return ok(monitor_request("DELETE", f"/stock-inbounds/{item_id}"))

    @app.get("/api/sales")
    def get_sales(date_: str | None = Query(None, alias="date")) -> JSONResponse:
        date_ = date_ or date.today().isoformat()
        return ok(monitor_request("GET", "/sales", params={"date": date_}))

    @app.get("/api/sales/dates")
    def get_sales_dates() -> JSONResponse:
        return ok(monitor_request("GET", "/sales/dates"))

    @app.get("/api/sales/series")
    def get_sales_series(start: str, end: str) -> JSONResponse:
        return ok(monitor_request("GET", "/sales/series", params={"start": start, "end": end}))

    @app.post("/api/sales/sync")
    def sync_sales() -> JSONResponse:
        return sync_channel("naver")

    @app.get("/api/revenue")
    def get_revenue(period_days: int = 30) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", "/revenue", params={"period_days": period_days}))
        require_monitor_for_vercel("revenue")
        snapshot, warnings = RevenueComparisonService(config).fetch(period_days)
        return ok({"snapshot": snapshot, "warnings": warnings})

    @app.post("/api/revenue/sync")
    def sync_revenue(period_days: int = 30) -> JSONResponse:
        if config.monitor_url:
            return start_job(
                "revenue-sync",
                lambda log, progress: (
                    log("requesting Raspberry Pi revenue refresh"),
                    progress(25),
                    monitor_request("POST", "/sync/revenue", json_body={"period_days": period_days}, timeout=90.0),
                )[-1],
            )
        require_monitor_for_vercel("revenue sync")
        return start_job(
            "revenue-sync",
            lambda log, progress: (
                log("revenue sync started"),
                progress(50),
                RevenueComparisonService(config).fetch(period_days),
            )[-1],
        )

    @app.get("/api/keywords")
    def get_keywords(period_days: int = 30) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", "/keywords", params={"period_days": period_days}))
        require_monitor_for_vercel("keywords")
        snapshot, warnings = NaverKeywordRevenueService(config).fetch(period_days)
        return ok({"snapshot": snapshot, "warnings": warnings})

    @app.post("/api/keywords/sync")
    def sync_keywords(period_days: int = 30) -> JSONResponse:
        if config.monitor_url:
            return start_job(
                "keyword-sync",
                lambda log, progress: (
                    log("requesting Raspberry Pi keyword refresh"),
                    progress(25),
                    monitor_request("POST", "/sync/keywords", json_body={"period_days": period_days}, timeout=90.0),
                )[-1],
            )
        require_monitor_for_vercel("keyword sync")
        return start_job(
            "keyword-sync",
            lambda log, progress: (
                log("keyword sync started"),
                progress(50),
                NaverKeywordRevenueService(config).fetch(period_days),
            )[-1],
        )

    @app.get("/api/purchases/records")
    def get_purchase_records(channel: str = "all", limit: int = 2000) -> JSONResponse:
        if config.monitor_url:
            params: dict[str, Any] = {"limit": limit}
            if channel != "all":
                params["channel"] = channel
            return ok(monitor_request("GET", "/purchase-records", params=params))
        store = PurchaseHistoryStore()
        return ok({"records": store.load_records(channel=channel, limit=limit)})

    @app.get("/api/purchases/orders")
    def get_purchase_orders(channel: str = "all", limit: int = 2000) -> JSONResponse:
        if config.monitor_url:
            params: dict[str, Any] = {"limit": limit}
            if channel != "all":
                params["channel"] = channel
            return ok(monitor_request("GET", "/purchase-orders", params=params))
        store = PurchaseHistoryStore()
        return ok({"orders": store.load_orders(channel=channel, limit=limit)})

    @app.post("/api/purchases/import/text")
    async def import_purchase_text(request: Request) -> JSONResponse:
        body = await request.json()
        channel = str(body.get("channel") or "coupang")
        text = str(body.get("text") or "")
        source_url = body.get("source_url") or "web-paste"
        parser = PurchaseHistoryParser()
        records = parser.parse_text(channel, text, source_url=source_url)
        store = purchase_store()
        added = store.save_records(records)
        return ok({"parsed": len(records), "saved": added})

    @app.post("/api/purchases/import/file")
    async def import_purchase_file(channel: str = "coupang", file: UploadFile = File(...)) -> JSONResponse:
        raw = await file.read()
        text = raw.decode("utf-8", errors="ignore")
        parser = PurchaseHistoryParser()
        records = parser.parse_text(channel, text, source_url=file.filename or "upload")
        added = purchase_store().save_records(records)
        return ok({"parsed": len(records), "saved": added, "filename": file.filename})

    @app.delete("/api/purchases/records")
    def delete_purchase_records(
        channel: str | None = None,
        missing_order_no: bool = False,
        order_no_like: str | None = None,
        title_like: str | None = None,
    ) -> JSONResponse:
        return ok(
            monitor_request(
                "DELETE",
                "/purchase-records",
                params={
                    "channel": channel,
                    "missing_order_no": "1" if missing_order_no else None,
                    "order_no_like": order_no_like,
                    "title_like": title_like,
                },
            )
        )

    @app.get("/api/purchases/coupang-credentials")
    def get_coupang_credentials() -> JSONResponse:
        return ok(monitor_request("GET", "/coupang-credentials"))

    @app.post("/api/purchases/coupang-credentials")
    async def save_coupang_credentials(request: Request) -> JSONResponse:
        return ok(monitor_request("POST", "/coupang-credentials", json_body=await request.json()))

    @app.delete("/api/purchases/coupang-credentials")
    def delete_coupang_credentials(label: str) -> JSONResponse:
        return ok(monitor_request("DELETE", "/coupang-credentials", params={"label": label}))

    @app.get("/api/cards/categories")
    def get_card_categories() -> JSONResponse:
        return ok(DEFAULT_CATEGORIES)

    @app.get("/api/cards/usages")
    def get_card_usages(
        start_date: str | None = None,
        end_date: str | None = None,
        card_num: str | None = None,
        limit: int = 5000,
    ) -> JSONResponse:
        if config.monitor_url:
            return ok(
                monitor_request(
                    "GET",
                    "/card-usages",
                    params={
                        "start_date": start_date,
                        "end_date": end_date,
                        "card_num": card_num,
                        "limit": limit,
                    },
                )
            )
        require_monitor_for_vercel("card usages")
        page = CardApiClient.from_config(config).list_card_usages(
            page=1,
            page_size=min(limit, 500),
            card_num=card_num,
            start_date=start_date,
            end_date=end_date,
        )
        return ok(page)

    @app.patch("/api/cards/usages/{usage_id}")
    async def patch_card_usage(usage_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("PATCH", f"/card-usages/{usage_id}", json_body=body))
        require_monitor_for_vercel("card usage update")
        client = CardApiClient.from_config(config)
        return ok(
            client.update_card_usage(
                usage_id,
                memo=body.get("memo"),
                category=body.get("category"),
                reviewed=body.get("reviewed") if "reviewed" in body else None,
                coupang_purchase_id=body.get("coupangPurchaseId") or body.get("coupang_purchase_id"),
            )
        )

    @app.post("/api/cards/sync")
    async def sync_cards(request: Request) -> JSONResponse:
        body = await request.json()

        def work(log: Callable[[str], None], progress: Callable[[int], None]) -> dict[str, Any]:
            log("card sync started")
            if config.monitor_url:
                progress(25)
                return monitor_request("POST", "/sync/card-usages", json_body=body, timeout=90.0)
            require_monitor_for_vercel("card sync")
            client = CardApiClient.from_config(config)
            progress(30)
            result = client.sync_card_usages(
                start_date=body.get("start_date") or body.get("startDate"),
                end_date=body.get("end_date") or body.get("endDate"),
                card_num=body.get("card_num") or body.get("cardNum"),
            )
            progress(90)
            return result

        return start_job("card-sync", work)

    @app.post("/api/cards/coupang-match")
    async def match_cards(request: Request) -> JSONResponse:
        body = await request.json()

        def work(log: Callable[[str], None], progress: Callable[[int], None]) -> dict[str, Any]:
            log("coupang card match started")
            if config.monitor_url:
                progress(25)
                return monitor_request(
                    "POST",
                    "/sync/coupang-purchases/match",
                    json_body=body,
                    timeout=90.0,
                )
            require_monitor_for_vercel("coupang purchase matching")
            client = CardApiClient.from_config(config)
            progress(30)
            result = client.match_coupang_purchases(
                start_date=body.get("start_date") or body.get("startDate"),
                end_date=body.get("end_date") or body.get("endDate"),
            )
            progress(90)
            return result

        return start_job("card-coupang-match", work)

    @app.get("/api/cards/fixed-costs")
    def get_fixed_costs() -> JSONResponse:
        return ok(monitor_request("GET", "/fixed-costs"))

    @app.post("/api/cards/fixed-costs")
    async def upsert_fixed_costs(request: Request) -> JSONResponse:
        return ok(monitor_request("POST", "/fixed-costs", json_body=await request.json()))

    @app.delete("/api/cards/fixed-costs/{item_id}")
    def delete_fixed_cost(item_id: int) -> JSONResponse:
        return ok(monitor_request("DELETE", f"/fixed-costs/{item_id}"))

    @app.get("/api/fassto/config")
    def get_fassto_config() -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", "/fassto/config"))
        require_monitor_for_vercel("fassto config")
        connector = make_fassto()
        return ok(connector.config_summary())

    @app.get("/api/fassto/goods")
    def get_fassto_goods(download: bool = False) -> Response:
        if config.monitor_url:
            payload = monitor_request("GET", "/fassto/goods")
        else:
            require_monitor_for_vercel("fassto goods")
            with make_fassto() as connector:
                payload = connector.get_goods_list()
        if download:
            return _csv_response(_as_list(payload), "fassto_goods.csv")
        return ok(payload)

    @app.get("/api/fassto/elements")
    def get_fassto_elements(download: bool = False) -> Response:
        if config.monitor_url:
            payload = monitor_request("GET", "/fassto/elements")
        else:
            require_monitor_for_vercel("fassto elements")
            with make_fassto() as connector:
                payload = connector.get_goods_elements()
        if download:
            return _csv_response(_as_list(payload), "fassto_elements.csv")
        return ok(payload)

    @app.get("/api/fassto/stock")
    def get_fassto_stock(download: bool = False) -> Response:
        if config.monitor_url:
            payload = monitor_request("GET", "/fassto/stock")
        else:
            require_monitor_for_vercel("fassto stock")
            with make_fassto() as connector:
                payload = connector.get_stock_list()
        if download:
            return _csv_response(_as_list(payload), "fassto_stock.csv")
        return ok(payload)

    @app.get("/api/fassto/warehousing")
    def get_fassto_warehousing(
        start: str | None = None,
        end: str | None = None,
        download: bool = False,
    ) -> Response:
        start = start or (date.today() - timedelta(days=30)).strftime("%Y%m%d")
        end = end or date.today().strftime("%Y%m%d")
        if config.monitor_url:
            payload = monitor_request("GET", "/fassto/warehousing", params={"start": start, "end": end})
        else:
            require_monitor_for_vercel("fassto warehousing")
            with make_fassto() as connector:
                payload = connector.get_warehousing_list(start, end)
        if download:
            return _csv_response(_as_list(payload), "fassto_warehousing.csv")
        return ok(payload)

    @app.get("/api/fassto/warehousing/{slip_no}")
    def get_fassto_warehousing_detail(slip_no: str) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", f"/fassto/warehousing/{slip_no}"))
        require_monitor_for_vercel("fassto warehousing detail")
        with make_fassto() as connector:
            return ok(connector.get_warehousing_detail(slip_no))

    @app.post("/api/fassto/warehousing")
    async def create_fassto_warehousing(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/fassto/warehousing", json_body=body, timeout=90.0))
        require_monitor_for_vercel("fassto warehousing create")
        payload = [build_warehousing_payload(item) for item in body.get("items", [body])]
        with make_fassto() as connector:
            return ok(connector.create_warehousing(payload))

    @app.patch("/api/fassto/warehousing")
    async def update_fassto_warehousing(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("PATCH", "/fassto/warehousing", json_body=body, timeout=90.0))
        require_monitor_for_vercel("fassto warehousing update")
        payload = [build_warehousing_payload(item) for item in body.get("items", [body])]
        with make_fassto() as connector:
            return ok(connector.update_warehousing(payload))

    @app.post("/api/fassto/warehousing/cancel")
    async def cancel_fassto_warehousing(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/fassto/warehousing/cancel", json_body=body, timeout=90.0))
        require_monitor_for_vercel("fassto warehousing cancel")
        with make_fassto() as connector:
            return ok(connector.cancel_warehousing(body.get("items", [body])))

    @app.get("/api/fassto/delivery")
    def get_fassto_delivery(
        start: str | None = None,
        end: str | None = None,
        status: str = "ALL",
        out_div: str = "1",
        download: bool = False,
    ) -> Response:
        start = start or (date.today() - timedelta(days=30)).strftime("%Y%m%d")
        end = end or date.today().strftime("%Y%m%d")
        if config.monitor_url:
            payload = monitor_request(
                "GET",
                "/fassto/delivery",
                params={"start": start, "end": end, "status": status, "out_div": out_div},
            )
        else:
            require_monitor_for_vercel("fassto delivery")
            with make_fassto() as connector:
                payload = connector.get_delivery_list(start, end, status=status, out_div=out_div)
        if download:
            return _csv_response(_as_list(payload), "fassto_delivery.csv")
        return ok(payload)

    @app.get("/api/fassto/delivery/{slip_no}")
    def get_fassto_delivery_detail(slip_no: str) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", f"/fassto/delivery/{slip_no}"))
        require_monitor_for_vercel("fassto delivery detail")
        with make_fassto() as connector:
            return ok(connector.get_delivery_detail(slip_no))

    @app.post("/api/fassto/delivery")
    async def create_fassto_delivery(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/fassto/delivery", json_body=body, timeout=90.0))
        require_monitor_for_vercel("fassto delivery create")
        with make_fassto() as connector:
            return ok(connector.create_delivery_parcel(body.get("items", [body])))

    @app.patch("/api/fassto/delivery")
    async def update_fassto_delivery(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("PATCH", "/fassto/delivery", json_body=body, timeout=90.0))
        require_monitor_for_vercel("fassto delivery update")
        with make_fassto() as connector:
            return ok(connector.update_delivery_parcel(body.get("items", [body])))

    @app.post("/api/fassto/delivery/cancel")
    async def cancel_fassto_delivery(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/fassto/delivery/cancel", json_body=body, timeout=90.0))
        require_monitor_for_vercel("fassto delivery cancel")
        with make_fassto() as connector:
            return ok(connector.cancel_delivery(body.get("items", [body])))

    @app.get("/api/fassto/parcels")
    def get_fassto_parcels(
        start: str | None = None,
        end: str | None = None,
        out_div: str = "1",
        download: bool = False,
    ) -> Response:
        start = start or (date.today() - timedelta(days=30)).strftime("%Y%m%d")
        end = end or date.today().strftime("%Y%m%d")
        if config.monitor_url:
            payload = monitor_request(
                "GET",
                "/fassto/parcels",
                params={"start": start, "end": end, "out_div": out_div},
            )
        else:
            require_monitor_for_vercel("fassto parcels")
            with make_fassto() as connector:
                payload = connector.get_delivery_parcel_list(start, end, out_div=out_div)
        if download:
            return _csv_response(_as_list(payload), "fassto_parcels.csv")
        return ok(payload)

    @app.get("/api/fassto/revenue")
    def get_fassto_revenue(
        start: str | None = None,
        end: str | None = None,
        download: bool = False,
    ) -> Response:
        start = start or (date.today() - timedelta(days=30)).strftime("%Y%m%d")
        end = end or date.today().strftime("%Y%m%d")
        if config.monitor_url:
            payload = monitor_request("GET", "/fassto/revenue", params={"start": start, "end": end})
        else:
            require_monitor_for_vercel("fassto revenue")
            with make_fassto() as connector:
                payload = connector.get_delivery_good_detail_list(start, end)
        if download:
            return _csv_response(_as_list(payload), "fassto_revenue.csv")
        return ok(payload)

    @app.get("/api/prefs")
    def get_pref(key: str) -> JSONResponse:
        if config.monitor_url:
            return ok(monitor_request("GET", "/ui-prefs", params={"key": key}))
        return ok({"key": key, "value": None})

    @app.post("/api/prefs")
    async def set_pref(request: Request) -> JSONResponse:
        body = await request.json()
        if config.monitor_url:
            return ok(monitor_request("POST", "/ui-prefs", json_body=body))
        return ok({"saved": False, "reason": "monitor backend is not configured"})

    @app.get("/api/export/{kind}")
    def export_kind(kind: str) -> Response:
        if kind == "naver":
            response = get_channel("naver")
            data = response.body
            return Response(data, media_type="application/json")
        if kind == "coupang":
            response = get_channel("coupang")
            data = response.body
            return Response(data, media_type="application/json")
        return fail("unknown export kind", status_code=404)

    return app


app = create_app()
