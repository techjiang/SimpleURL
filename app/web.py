"""页面渲染（Jinja2）。"""

from __future__ import annotations

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.core.config import settings
from app.core.csrf import current_token
from app.core.timeutil import ensure_aware, fmt_local, iso, local_naive

templates = Jinja2Templates(directory=str(settings.templates_dir))


def _csrf_token(request) -> str:
    """模板里取 CSRF 令牌用：get_template().render() 不走 Request 上下文也能拿到。"""
    return current_token(request)


def _csp_nonce(request) -> str:
    return getattr(getattr(request, "state", None), "csp_nonce", "") or ""


templates.env.globals.update(csrf_token=_csrf_token, csp_nonce=_csp_nonce)


def _fmt(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return fmt_local(value, fmt)


def _local_input(value) -> str:
    """转成 datetime-local 输入框要的 naive 本地时间串。"""
    naive = local_naive(value)
    return naive.strftime("%Y-%m-%dT%H:%M") if naive else ""


def branding(db=None) -> dict[str, str]:
    """站点名称与自述：数据库里的覆盖优先，环境变量只是出厂默认。

    原来模板全局只绑了环境变量，后台「设置 → 站点名称」改完存进库、
    接口也读得到，页面标题和页脚却纹丝不动——看着像保存失败。
    """
    from app.core.settings_store import effective

    site_name = str(effective(db, "site_name") or settings.site_name or "SimpleURL").strip()
    tagline = str(effective(db, "site_tagline") or settings.site_tagline or "").strip()
    return {"site_name": site_name or "SimpleURL", "site_tagline": tagline}


def render(template: str, request: Request, db=None, **context) -> str:
    """统一渲染入口：顺手把品牌信息塞进去。"""
    ctx = {**branding(db), **context}
    return templates.get_template(template).render(request=request, **ctx)


def configured_base(db=None) -> str:
    """后台「站点设置 → 对外地址」优先，其次环境变量 PUBLIC_URL。"""
    from app.core.settings_store import effective

    value = ""
    if db is not None:
        try:
            value = str(effective(db, "public_url") or "").strip()
        except Exception:  # noqa: BLE001 - 没库 / 没表时不该让页面挂掉
            value = ""
    return (value or settings.public_url or "").rstrip("/")


def site_base(request: Request, db=None) -> str:
    """站点对外地址：配了就用它，否则用请求里的 Host。

    反代后面请求里的 Host 常常是 127.0.0.1:8000，直接拼给访客就是一个打不开的
    地址，所以「站点对外的样子」交给部署者说了算。
    """
    return configured_base(db) or str(request.base_url).rstrip("/")


def render_stats(request: Request, link, data: dict, db=None) -> str:
    base = site_base(request, db)
    # 二维码也要带同一张通行证，否则统计页上那张图会 401。
    # query 里没有就看 cookie——访问过一次之后地址栏会被收拾干净，
    # 二维码和「换一张回执」都还得认得这位创建者。
    from app.core.middleware import CLAIM_COOKIE

    claim = request.query_params.get("t") or request.cookies.get(CLAIM_COOKIE) or ""
    # 回执只对**它自己那条**链接有效。cookie 里那张通常是最近一次创建 / 换新
    # 留下的，和管理员会话一样是「人身上的」而不是「链接上的」：管理员在后台
    # 打开 A 的统计页，页面上的二维码却会带着 B 的回执去请求，
    # 服务端按「回执不属于这条链接」拒绝，图就裂了——一张裂图在页面上
    # 看起来纯粹是坏了，没人猜得到是回执串了链接。
    from app.core.claim import verify as _claim_verify

    if claim and not _claim_verify(db, link, claim):
        claim = ""
    # 二维码地址带上尺寸和回执：统计页上那张图是给人看的 140px，
    # 不指定尺寸时服务端按默认档出图，图片被浏览器缩到 140 反而糊
    qr_qs = [f"size=320"]
    if claim:
        qr_qs.append(f"t={claim}")
    qr_src = f"{base}/api/qr/{link.code}?" + "&".join(qr_qs)
    return render(
        "stats.html",
        request,
        db,
        link=link,
        data=data,
        short_url=base + "/" + link.code,
        qr_src=qr_src,
        claim=claim,
    )


templates.env.filters.update(
    fmt=_fmt, fmt_local=fmt_local, local_input=_local_input, iso=iso, aware=ensure_aware
)
