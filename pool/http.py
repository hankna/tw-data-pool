"""單一傳輸模組：網域白名單、逾時 40 秒、重試 2 次、回應上限 8 MB、不跟隨跨網域轉址。
金鑰只從環境變數 FINMIND_TOKEN 讀取，任何輸出與紀錄都不含金鑰。"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from .util import SourceError

ALLOWED = {
    "api.finmindtrade.com",
    "www.twse.com.tw",
    "www.tpex.org.tw",
    "openapi.taifex.com.tw",
    "www.taifex.com.tw",
    "rate.bot.com.tw",
}
MAX_BYTES = 8 * 1024 * 1024
FINMIND = "https://api.finmindtrade.com/api/v4/data"


class HttpStatus(SourceError):
    def __init__(self, code, msg=""):
        super().__init__(f"HTTP {code} {msg}".strip())
        self.code = code


class _NoCrossHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            raise SourceError("拒絕跨網域轉址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, token: str | None = None, timeout: int = 40, retries: int = 2):
        self.token = token if token is not None else os.environ.get("FINMIND_TOKEN") or None
        self.token_state = "none" if not self.token else "valid"
        self.last_headers = {}   # 最近一次 bytes() 回應的標頭（小寫鍵）；臺銀 CSV 靠檔名核對資料日
        self.timeout, self.retries = timeout, retries
        self._opener = urllib.request.build_opener(_NoCrossHost)

    # ---- 底層 ----
    def bytes(self, url: str, params: dict | None = None, post: dict | None = None) -> bytes:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        host = urllib.parse.urlsplit(url).hostname
        if host not in ALLOWED:
            raise SourceError(f"網域不在白名單：{host}")
        data = urllib.parse.urlencode(post).encode() if post else None
        last = None
        for i in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "tw-data-pool/2"})
                with self._opener.open(req, timeout=self.timeout) as r:
                    body = r.read(MAX_BYTES + 1)
                    self.last_headers = {k.lower(): v for k, v in r.headers.items()}
                if len(body) > MAX_BYTES:
                    raise SourceError("回應超過 8 MB")
                return body
            except SourceError:
                raise
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code != 429:
                    raise HttpStatus(e.code, host) from None     # 4xx 重試無益
                last = e
                time.sleep(2 * (i + 1))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (i + 1))
        raise SourceError(f"連線失敗：{host}：{type(last).__name__}")

    def json(self, url: str, params: dict | None = None):
        raw = self.bytes(url, params)
        if not raw.strip():
            raise SourceError("空回應")
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:
            raise SourceError(f"非 JSON：{e}") from None

    # ---- FinMind ----
    def finmind(self, dataset: str, data_id: str | None, start: str, end: str) -> list:
        p = {"dataset": dataset, "start_date": start, "end_date": end}
        if data_id:
            p["data_id"] = data_id
        if self.token and self.token_state != "invalid":
            try:
                j = self.json(FINMIND, {**p, "token": self.token})
                bad = j.get("status") != 200 and "oken" in str(j.get("msg", ""))
            except HttpStatus as e:
                bad = e.code in (400, 401, 403)
            if bad:
                self.token_state = "invalid"   # 金鑰失效：本次改免金鑰
                j = self.json(FINMIND, p)
        else:
            j = self.json(FINMIND, p)
        if j.get("status") != 200:
            raise SourceError(f"FinMind {dataset}：{str(j.get('msg'))[:60]}")
        return j.get("data") or []
