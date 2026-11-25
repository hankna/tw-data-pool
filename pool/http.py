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


def _origin(url: str) -> tuple:
    u = urllib.parse.urlsplit(url)
    return u.scheme.lower(), u.hostname, u.port or {"http": 80, "https": 443}.get(u.scheme.lower())


class _NoCrossHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = _origin(req.full_url), _origin(urllib.parse.urljoin(req.full_url, newurl))
        if new[1] != old[1]:
            raise SourceError("拒絕跨網域轉址")
        if old[0] == "https" and new[0] != "https":
            raise SourceError("拒絕降級為 HTTP 的轉址")
        # urllib 會把 Authorization 複製到轉址請求：帶金鑰時只允許同來源（scheme、主機、連接埠都相同）的 HTTPS
        if req.has_header("Authorization") and (new != old or new[0] != "https"):
            raise SourceError("帶金鑰時拒絕非同來源 HTTPS 的轉址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, token: str | None = None, timeout: int = 40, retries: int = 2):
        self.token = token if token is not None else os.environ.get("FINMIND_TOKEN") or None
        self.token_state = "none" if not self.token else "valid"
        self.token_reason = ""
        self.last_headers = {}   # 最近一次 bytes() 回應的標頭（小寫鍵）；臺銀 CSV 靠檔名核對資料日
        self.timeout, self.retries = timeout, retries
        self._opener = urllib.request.build_opener(_NoCrossHost)

    # ---- 底層 ----
    def bytes(self, url: str, params: dict | None = None, post: dict | None = None,
              headers: dict | None = None) -> bytes:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        host = urllib.parse.urlsplit(url).hostname
        if host not in ALLOWED:
            raise SourceError(f"網域不在白名單：{host}")
        data = urllib.parse.urlencode(post).encode() if post else None
        last = None
        for i in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "tw-data-pool/2", **(headers or {})})
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

    def json(self, url: str, params: dict | None = None, headers: dict | None = None):
        raw = self.bytes(url, params, headers=headers)
        if not raw.strip():
            raise SourceError("空回應")
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:
            raise SourceError(f"非 JSON：{e}") from None

    def _token_invalid(self, why: str, dataset: str) -> None:
        self.token_state = "invalid"   # 金鑰失效：本次後續請求改免金鑰
        self.token_reason = f"{why}（{dataset}）"

    # ---- FinMind ----
    def finmind(self, dataset: str, data_id: str | None, start: str, end: str) -> list:
        p = {"dataset": dataset, "start_date": start, "end_date": end}
        if data_id:
            p["data_id"] = data_id
        if self.token and self.token_state != "invalid":
            sure = False   # 確定是金鑰被拒（401／403，或回應訊息提到 token）
            try:
                # FinMind v4 以 Authorization: Bearer 標頭認證；不放網址參數，金鑰才不會出現在網址
                j = self.json(FINMIND, p, headers={"Authorization": f"Bearer {self.token}"})
                st = j.get("status")
                bad = sure = st != 200 and "oken" in str(j.get("msg", ""))
                # 公開資料分支只記代碼，不記 FinMind 回傳的訊息文字（可能回顯金鑰，截斷或編碼後遮蔽會失效）
                why = f"FinMind status {st}" if isinstance(st, int) and not isinstance(st, bool) else "FinMind 拒絕金鑰"
            except HttpStatus as e:
                if e.code not in (400, 401, 403):
                    raise                                   # 其他狀態原樣拋出，不動金鑰狀態
                bad, sure, why = True, e.code in (401, 403), f"HTTP {int(e.code)}"
            if bad:
                # 改免金鑰重試；400 只有在免金鑰成功時才判定是金鑰的問題（一般參數錯誤也會回 400）
                try:
                    anon = self.json(FINMIND, p)
                except SourceError:
                    if sure:
                        self._token_invalid(why, dataset)
                    raise
                if sure or anon.get("status") == 200:
                    self._token_invalid(why, dataset)
                j = anon
        else:
            j = self.json(FINMIND, p)
        if j.get("status") != 200:
            st = j.get("status")
            code = st if isinstance(st, int) and not isinstance(st, bool) else "?"
            # 錯誤會進公開 digest：不論有無金鑰都只留資料集與狀態碼，不照錄上游訊息
            raise SourceError(f"FinMind {dataset}：status {code}")
        return j.get("data") or []
