"""用最小桩件导入真实 FastAPI 应用，核验路由表与领域接线（不依赖第三方包）。"""

import sys
import types
from datetime import date


# ---- 轻量桩：仅覆盖被测代码用到的接口 ----
def _make_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# pydantic
pydantic = _make_module("pydantic")


class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def model_dump(self):
        return self.__dict__


def Field(*a, **kw):
    return None


def field_validator(*a, **kw):
    def deco(fn):
        return classmethod(fn)
    return deco


pydantic.BaseModel = BaseModel
pydantic.Field = Field
pydantic.field_validator = field_validator

# fastapi 及子模块
fastapi = _make_module("fastapi")


class HTTPException(Exception):
    def __init__(self, status_code=400, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class APIRouter:
    def __init__(self, prefix="", tags=None):
        self.prefix = prefix
        self.routes = []

    def _record(self, path, methods, **kw):
        def deco(fn):
            self.routes.append((self.prefix + path, methods, fn.__name__))
            return fn
        return deco

    def get(self, path, **kw):
        return self._record(path, {"GET"}, **kw)

    def post(self, path, **kw):
        return self._record(path, {"POST"}, **kw)


def Depends(fn):
    return fn


def Query(*a, **kw):
    return kw.get("default")


fastapi.Query = Query
fastapi.FastAPI = lambda *a, **kw: types.SimpleNamespace(
    middleware=lambda *x, **y: (lambda fn: fn), include_router=lambda r: REGISTERED.append(r))
fastapi.APIRouter = APIRouter
fastapi.Depends = Depends
fastapi.HTTPException = HTTPException

security = _make_module("fastapi.security")
security.HTTPAuthorizationCredentials = object
security.HTTPBearer = lambda **kw: None

responses = _make_module("fastapi.responses")
responses.JSONResponse = lambda *a, **kw: None
responses.Response = type("Response", (), {})
fastapi.Response = responses.Response

# sqlalchemy 桩
sqla = _make_module("sqlalchemy")


class _Col:
    def __init__(self, *a, **kw):
        pass

    def in_(self, x):
        return self

    def where(self, *a):
        return self

    def order_by(self, *a):
        return self

    def limit(self, n):
        return self

    def with_for_update(self, *a, **kw):
        return self

    def label(self, x):
        return self

    def __eq__(self, other):
        return self

    def __ne__(self, other):
        return self

    def __and__(self, other):
        return self


def Column(*a, **kw):
    return _Col()


class _Table:
    def __init__(self, name, metadata, *cols, **kw):
        self.name = name
        self.c = types.SimpleNamespace()


def Table(name, metadata, *cols, **kw):
    return _Table(name, metadata, *cols, **kw)


class MetaData:
    pass


def select(*a):
    return _Col()


def insert(t):
    return _Col()


def update(t):
    return _Col()


def and_(*a):
    return a[0]


def or_(*a):
    return a[0]


def text(x):
    return x


class CheckConstraint:
    def __init__(self, *a, **kw):
        pass


class _Func:
    def __getattr__(self, name):
        return lambda *a: _Col()


func = _Func()
sqla.func = func
sqla.create_engine = lambda *a, **kw: types.SimpleNamespace(dialect=types.SimpleNamespace(name="postgresql"), begin=None, connect=None, execute=None)
sqla.Column = Column
sqla.Table = Table
sqla.MetaData = MetaData
sqla.select = select
sqla.insert = insert
sqla.update = update
sqla.and_ = and_
sqla.or_ = or_
sqla.text = text
sqla.CheckConstraint = CheckConstraint
for tname in ("String", "Integer", "BigInteger", "JSON", "ForeignKey"):
    setattr(sqla, tname, lambda *a, **kw: _Col())

exc = _make_module("sqlalchemy.exc")
exc.IntegrityError = type("IntegrityError", (Exception,), {})
exc.SQLAlchemyError = type("SQLAlchemyError", (Exception,), {})
dia = _make_module("sqlalchemy.dialects")
pg = _make_module("sqlalchemy.dialects.postgresql")
pg.insert = lambda *a, **kw: None
my = _make_module("sqlalchemy.dialects.mysql")
my.insert = lambda *a, **kw: None

# redis
redis = _make_module("redis")
redis.Redis = type("Redis", (), {"from_url": staticmethod(lambda *a, **kw: object())})
redis_exc = _make_module("redis.exceptions")
redis_exc.RedisError = type("RedisError", (Exception,), {})

# jwt
jwt = _make_module("jwt")
jwt.decode = lambda *a, **kw: {"sub": "operator", "role": "operator", "exp": 1}
jwt.InvalidTokenError = type("InvalidTokenError", (Exception,), {})

# 项目内资源/配置桩
sys.path.insert(0, "/workspace/gb-6105/backend")
import os
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET", "x")
os.environ.setdefault("DEMO_PASSWORD", "x")

REGISTERED = []

# 避免 lifespan 触发真实建表/seed
import scripts.seed_demo as seed_mod  # noqa: E402
seed_mod.setup_schema = lambda: None
seed_mod.seed = lambda: {}

from src.main import app  # noqa: E402
from src.routes import transfers as transfer_routes  # noqa: E402

paths = []
for router in REGISTERED:
    for path, methods, fn in router.routes:
        paths.append((",".join(sorted(methods)), path, fn))

print("已注册路由：")
for methods, path, fn in sorted(paths, key=lambda x: x[1]):
    print(f"  {methods:6s} {path:42s} -> {fn}")

expected = {
    "/transfers/dossiers",
    "/transfers/orders",
    "/transfers/orders/{order_id}",
    "/transfers/orders/{order_id}/verification",
    "/transfers/point-check",
}
seen = {p for _, p, _ in paths}
missing = expected - seen
assert not missing, f"缺少路由：{missing}"
assert any(p == "/transfers/orders" and m == "POST" for m, p, _ in paths)

# 核验领域模块仍可正常工作（真实代码，无桩）
from src.domain.transfer import build_plan, parse_request, point_check, apply_plan, Dossier  # noqa: E402
from src.domain.certificates import Certificate  # noqa: E402

d1 = Dossier(1, "DOS-A", "甲", 4, {f"YT-Q0{i}": Certificate(
    f"YT-Q0{i}", f"YT-L0{i}", date(2026, 12, 31), False) for i in range(1, 5)})
d2 = Dossier(2, "DOS-B", "乙", 4, {f"YT-Q0{i}": Certificate(
    f"YT-Q0{i}", f"YT-L0{i}", date(2026, 12, 31), False) for i in range(5, 9)})
plan = build_plan({1: d1, 2: d2}, {
    "left_dossier_id": 1, "right_dossier_id": 2,
    "left_codes": ["YT-Q01"], "right_codes": ["YT-Q05"]})
after = apply_plan({1: d1, 2: d2}, plan)
assert "YT-Q05" in after[1].certificates and "YT-Q01" in after[2].certificates
print("\n领域互换断言通过；应用路由装配成功。")
