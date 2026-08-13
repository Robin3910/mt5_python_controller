"""客户端版本管理 API（运维管理链路）。

职责边界：本模块只做鉴权、落盘编排与审计，版本比较与安装包结构校验全部委托给
纯函数模块 `client_version`；包体存磁盘（settings.client_package_dir），元数据存
MySQL，「当前发布哪个版本」存 system_setting.client_release 指针。

两套鉴权刻意分开：
- 管理端（JWT）——上传、发布、降级、回滚、删除；
- 节点端（NODE_TOKEN）——只有查发布版本与下载安装包两个只读接口。
  NODE_TOKEN 全局共享且明文分发到每台节点机，不能用它授权任何写操作。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select

from . import client_version as cv
from . import persist, system_settings
from .db import SessionLocal
from .deps import client_ip, get_current_admin, get_node_token_auth
from .models import (
    ClientReleaseOut,
    ClientReleasePayload,
    ClientVersionListOut,
    ClientVersionOut,
)
from .orm import ClientVersion, Node
from .settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/client-versions", tags=["client-version"])

# 上传时的分块大小：单 worker 部署，整包读进内存会占死进程
_CHUNK = 1 << 20


def package_dir() -> Path:
    """安装包存放目录（不存在则创建）。"""
    d = Path(settings.client_package_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def package_path(version: str) -> Path:
    """版本对应的包路径；version 必须是 normalize_version 的产物。"""
    return package_dir() / f"node_client-{version}.zip"


def _inspect_package(path: Path) -> tuple[list[str], str]:
    """读取 zip 成员清单与包内 version.txt 里的版本号（阻塞，调用方丢线程池）。"""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        member = cv.find_member(names, "version.txt")
        if not member:
            return names, ""
        return names, cv.version_from_txt(zf.read(member).decode("utf-8-sig", "replace"))


async def _node_version_distribution() -> tuple[dict[str, int], int]:
    """统计各版本的节点数，以及未上报版本的节点数。"""
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Node.client_version, func.count()).group_by(Node.client_version)
            )
        ).all()
    dist: dict[str, int] = {}
    unknown = 0
    for version, count in rows:
        key = (version or "").strip()
        if key:
            dist[key] = dist.get(key, 0) + int(count)
        else:
            unknown += int(count)
    return dist, unknown


def _release_out(release: dict) -> ClientReleaseOut:
    return ClientReleaseOut(
        version=str(release.get("version") or ""),
        previous=str(release.get("previous") or ""),
        updated_at=float(release.get("updated_at") or 0),
    )


@router.get("", response_model=ClientVersionListOut)
async def list_versions(_: str = Depends(get_current_admin)):
    """版本清单（新版在前）+ 当前发布指针 + 各版本的节点数分布。"""
    release = await system_settings.get_client_release()
    current = str(release.get("version") or "")
    dist, unknown = await _node_version_distribution()

    async with SessionLocal() as s:
        rows = (await s.execute(select(ClientVersion))).scalars().all()

    items = [
        ClientVersionOut(
            version=r.version,
            filename=r.filename,
            size=int(r.size or 0),
            sha256=r.sha256 or "",
            notes=r.notes,
            uploaded_by=r.uploaded_by or "",
            created_at=r.created_at.timestamp() if r.created_at else 0,
            is_current=(r.version == current),
            node_count=dist.get(r.version, 0),
        )
        for r in rows
    ]
    # 语义版本序，新版在前；同版本号不会出现（version 是主键）
    items.sort(key=lambda i: cv.parse_version(i.version) or (0,), reverse=True)
    return ClientVersionListOut(
        items=items, release=_release_out(release), unknown_node_count=unknown
    )


@router.post("", response_model=ClientVersionOut, status_code=201)
async def upload_version(
    request: Request,
    file: UploadFile = File(..., description="客户端安装包 zip"),
    version: str = Form("", description="留空则读包内 version.txt"),
    notes: str = Form("", description="更新说明"),
    admin: str = Depends(get_current_admin),
):
    """上传客户端安装包。

    包体先分块落到 .part 临时文件（边写边算 sha256），校验通过后才改名为正式文件，
    避免半个损坏的包被当成可发布版本。
    """
    limit = max(1, int(settings.client_package_max_mb)) * 1024 * 1024
    tmp = package_dir() / f".upload-{int(time.time() * 1000)}.part"
    digest = hashlib.sha256()
    size = 0

    try:
        with tmp.open("wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail=f"安装包超过 {settings.client_package_max_mb} MB 上限",
                    )
                digest.update(chunk)
                out.write(chunk)

        if size == 0:
            raise HTTPException(status_code=400, detail="安装包为空")

        try:
            names, pkg_version = await asyncio.to_thread(_inspect_package, tmp)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="不是有效的 zip 安装包")

        ok, reason = cv.validate_package(names)
        if not ok:
            raise HTTPException(status_code=400, detail=reason)

        raw_version = (version or "").strip() or pkg_version
        norm = cv.normalize_version(raw_version)
        if not norm:
            detail = (
                "版本号无效（仅允许字母数字与 . _ -）"
                if raw_version
                else "安装包内没有 version.txt，请在上传时手动填写版本号"
            )
            raise HTTPException(status_code=400, detail=detail)

        target = package_path(norm)
        async with SessionLocal() as s:
            if await s.get(ClientVersion, norm) is not None:
                raise HTTPException(status_code=409, detail=f"版本 {norm} 已存在")

            os.replace(tmp, target)
            row = ClientVersion(
                version=norm,
                filename=target.name,
                size=size,
                sha256=digest.hexdigest(),
                notes=(notes or "").strip() or None,
                uploaded_by=admin,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
    finally:
        tmp.unlink(missing_ok=True)

    after = {"version": norm, "size": size, "sha256": row.sha256}
    await persist.audit(
        admin, "upload_client_version", norm, {"filename": row.filename}, "ok",
        client_ip(request), category="system", before=None, after=after,
    )
    logger.info("client package uploaded: %s (%s bytes) by %s", norm, size, admin)
    return ClientVersionOut(
        version=row.version,
        filename=row.filename,
        size=size,
        sha256=row.sha256,
        notes=row.notes,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at.timestamp() if row.created_at else time.time(),
        is_current=False,
        node_count=0,
    )


@router.post("/rollback", response_model=ClientReleaseOut)
async def rollback_release(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """把发布指针回退到上一个发布过的版本（服务端回滚）。"""
    current = await system_settings.get_client_release()
    nxt = cv.rollback_release(current)
    if nxt is None:
        raise HTTPException(status_code=409, detail="没有可回滚的历史版本")

    async with SessionLocal() as s:
        if await s.get(ClientVersion, nxt["version"]) is None:
            raise HTTPException(
                status_code=409, detail=f"上一版本 {nxt['version']} 的安装包已被删除，无法回滚"
            )

    await system_settings.set_client_release(nxt)
    await persist.audit(
        admin, "rollback_client_release", nxt["version"], None, "ok",
        client_ip(request), category="system", before=current, after=nxt,
    )
    logger.warning("client release rolled back to %s by %s", nxt["version"], admin)
    return _release_out(nxt)


@router.post("/{version}/release", response_model=ClientReleaseOut)
async def release_version(
    version: str,
    request: Request,
    body: ClientReleasePayload | None = None,
    admin: str = Depends(get_current_admin),
):
    """把某个版本设为当前发布版本；判定为降级时必须显式确认。"""
    norm = cv.normalize_version(version)
    if not norm:
        raise HTTPException(status_code=400, detail="版本号无效")

    async with SessionLocal() as s:
        if await s.get(ClientVersion, norm) is None:
            raise HTTPException(status_code=404, detail=f"版本 {norm} 不存在")

    current = await system_settings.get_client_release()
    kind = cv.classify_update(str(current.get("version") or ""), norm)
    if kind == cv.DOWNGRADE and not (body and body.confirm_downgrade):
        raise HTTPException(
            status_code=409,
            detail=f"{norm} 低于当前发布版本 {current.get('version')}，降级需显式确认",
        )

    nxt = cv.build_release(norm, current)
    await system_settings.set_client_release(nxt)
    await persist.audit(
        admin, "release_client_version", norm, {"kind": kind}, "ok",
        client_ip(request), category="system", before=current, after=nxt,
    )
    logger.info("client release set to %s (%s) by %s", norm, kind, admin)
    return _release_out(nxt)


@router.delete("/{version}")
async def delete_version(
    version: str,
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """删除某个版本的安装包；当前发布版本禁止删除。"""
    norm = cv.normalize_version(version)
    if not norm:
        raise HTTPException(status_code=400, detail="版本号无效")

    release = await system_settings.get_client_release()
    if release.get("version") == norm:
        raise HTTPException(status_code=409, detail="不能删除当前发布版本，请先切换到其它版本")

    async with SessionLocal() as s:
        row = await s.get(ClientVersion, norm)
        if row is None:
            raise HTTPException(status_code=404, detail=f"版本 {norm} 不存在")
        before = {"version": row.version, "filename": row.filename, "sha256": row.sha256}
        await s.execute(delete(ClientVersion).where(ClientVersion.version == norm))
        await s.commit()

    package_path(norm).unlink(missing_ok=True)

    # 回滚落点指向已删除的包会让回滚必然失败，这里一并清掉
    if release.get("previous") == norm:
        cleared = dict(release)
        cleared["previous"] = ""
        await system_settings.set_client_release(cleared)

    await persist.audit(
        admin, "delete_client_version", norm, None, "ok",
        client_ip(request), category="system", before=before, after=None,
    )
    logger.info("client package deleted: %s by %s", norm, admin)
    return {"status": "ok", "version": norm}


@router.get("/available")
async def available_versions(_: str = Depends(get_node_token_auth)):
    """节点/面板列出可下载的版本（新版在前）。

    供运维面板的「导入节点」与「更新到指定版本」选版本用。只给下载所需的字段，
    不带上传者等管理信息 —— 节点令牌是全局共享的运维凭据，不该看到后台操作痕迹。
    """
    release = await system_settings.get_client_release()
    current = str(release.get("version") or "")

    async with SessionLocal() as s:
        rows = (await s.execute(select(ClientVersion))).scalars().all()

    items = [
        {
            "version": r.version,
            "size": int(r.size or 0),
            "sha256": r.sha256 or "",
            "notes": r.notes or "",
            "created_at": r.created_at.timestamp() if r.created_at else 0,
            "is_current": r.version == current,
        }
        for r in rows
    ]
    items.sort(key=lambda i: cv.parse_version(i["version"]) or (0,), reverse=True)
    return {"items": items, "current": current}


@router.get("/current")
async def current_version(_: str = Depends(get_node_token_auth)):
    """节点/面板查询当前应当安装的版本；尚未发布任何版本时返回 204。"""
    release = await system_settings.get_client_release()
    version = str(release.get("version") or "")
    if not version:
        return Response(status_code=204)

    async with SessionLocal() as s:
        row = await s.get(ClientVersion, version)
    if row is None:
        # 发布指针指向的包被删了：当作未发布，避免面板拿到一个下载必 404 的版本
        logger.warning("client release points to missing package: %s", version)
        return Response(status_code=204)

    return {
        "version": row.version,
        "filename": row.filename,
        "size": int(row.size or 0),
        "sha256": row.sha256 or "",
        "notes": row.notes or "",
        "updated_at": float(release.get("updated_at") or 0),
        "download_url": f"/api/client-versions/{row.version}/download",
    }


@router.get("/{version}/download")
async def download_version(version: str, _: str = Depends(get_node_token_auth)):
    """下载指定版本的安装包。"""
    norm = cv.normalize_version(version)
    if not norm:
        raise HTTPException(status_code=400, detail="版本号无效")

    async with SessionLocal() as s:
        row = await s.get(ClientVersion, norm)
    if row is None:
        raise HTTPException(status_code=404, detail=f"版本 {norm} 不存在")

    path = package_path(norm)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"版本 {norm} 的安装包文件已丢失")
    return FileResponse(path, media_type="application/zip", filename=row.filename)
