"""تخزين الملفات المرفوعة في Supabase Storage.

نظام ملفات Vercel للقراءة فقط وغير مشترك بين الطلبات، لذلك تُحفظ الملفات في حاوية
Supabase خاصة. الدالة path() تنزّل نسخة مؤقتة إلى /tmp حتى يستطيع pandas قراءتها.
"""
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.core.files import File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

TIMEOUT = 60


@deconstructible
class SupabaseStorage(Storage):
    def __init__(self, bucket=None):
        self.base = os.environ["SUPABASE_URL"].rstrip("/") + "/storage/v1"
        self.key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.bucket = bucket or os.environ.get("SUPABASE_STORAGE_BUCKET", "media")
        self.cache_dir = Path(tempfile.gettempdir()) / "supabase-media" / self.bucket

    # ------------------------------------------------------------ HTTP
    def _request(self, method, endpoint, data=None, headers=None, json_body=None):
        headers = {"Authorization": f"Bearer {self.key}", "apikey": self.key, **(headers or {})}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = Request(f"{self.base}/{endpoint}", data=data, headers=headers, method=method)
        with urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()

    def _object(self, name):
        return f"object/{self.bucket}/{quote(name, safe='/')}"

    def ensure_bucket(self):
        try:
            self._request("GET", f"bucket/{self.bucket}")
        except HTTPError as exc:
            if exc.code not in (400, 404):
                raise
            self._request("POST", "bucket", json_body={"id": self.bucket, "name": self.bucket, "public": False})

    # ------------------------------------------------------------ Storage API
    def get_valid_name(self, name):
        # Supabase يرفض المفاتيح غير اللاتينية (أسماء الملفات العربية)؛ الاسم الأصلي محفوظ في file_name
        ext = PurePosixPath(name).suffix.lower()
        if not ext.isascii():
            ext = ""
        return f"{uuid.uuid4().hex}{ext}"

    def _save(self, name, content):
        if hasattr(content, "seek"):
            content.seek(0)
        data = b"".join(content.chunks()) if hasattr(content, "chunks") else content.read()
        self._request("POST", self._object(name), data=data,
                      headers={"Content-Type": "application/octet-stream", "x-upsert": "true"})
        local = self.cache_dir / name
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return name

    def path(self, name):
        local = self.cache_dir / name
        if not local.exists():
            try:
                data = self._request("GET", self._object(name))
            except HTTPError as exc:
                if exc.code in (400, 404):
                    raise FileNotFoundError(name) from exc
                raise
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(local)
        return str(local)

    def _open(self, name, mode="rb"):
        return File(open(self.path(name), mode), name=name)

    def exists(self, name):
        if (self.cache_dir / name).exists():
            return True
        try:
            self._request("HEAD", self._object(name))
            return True
        except HTTPError as exc:
            if exc.code in (400, 404):
                return False
            raise

    def delete(self, name):
        if not name:
            return
        (self.cache_dir / name).unlink(missing_ok=True)
        self._request("DELETE", f"object/{self.bucket}", json_body={"prefixes": [name]})

    def size(self, name):
        return os.path.getsize(self.path(name))

    def _list(self, prefix, search=""):
        body = {"prefix": prefix.strip("/"), "limit": 1000, "offset": 0}
        if search:
            body["search"] = search
        return json.loads(self._request("POST", f"object/list/{self.bucket}", json_body=body) or b"[]")

    def listdir(self, path):
        dirs, files = [], []
        for item in self._list(path):
            (files if item.get("id") else dirs).append(item["name"])
        return dirs, files

    def get_modified_time(self, name):
        p = PurePosixPath(name)
        for item in self._list(str(p.parent), search=p.name):
            if item.get("name") == p.name and item.get("updated_at"):
                return datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc)

    def url(self, name):
        data = json.loads(self._request("POST", f"object/sign/{self.bucket}/{quote(name, safe='/')}",
                                        json_body={"expiresIn": 3600}))
        return f"{self.base}{data['signedURL']}"
