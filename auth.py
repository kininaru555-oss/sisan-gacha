from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db import db_transaction
from models import LoginRequest, RegisterRequest
from security import (
    clear_auth_cookies,
    issue_login_session,
    register_user,
    revoke_current_refresh_session,
    rotate_refresh_session,
    set_auth_cookies,
)
from utils import client_ip


router = APIRouter()


@router.post("/auth/register")
def auth_register(req: RegisterRequest):
    with db_transaction() as (conn, _):
        created = register_user(conn, user_id=req.user_id, password=req.password)
        return {
            "status": "ok",
            "user_id": created["user_id"],
            "role": created["role"],
        }


@router.post("/auth/login")
def auth_login(req: LoginRequest, request: Request):
    with db_transaction() as (conn, _):
        data = issue_login_session(
            conn,
            user_id=req.user_id,
            password=req.password,
            user_agent=request.headers.get("user-agent"),
            ip_address=client_ip(request),
        )
        response = JSONResponse({
            "status": "ok",
            "user_id": data["user_id"],
            "role": data["role"],
        })
        set_auth_cookies(
            response,
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            csrf_token=data["csrf_token"],
        )
        return response


@router.post("/auth/refresh")
def auth_refresh(request: Request):
    with db_transaction() as (conn, _):
        data = rotate_refresh_session(
            conn,
            request=request,
            user_agent=request.headers.get("user-agent"),
            ip_address=client_ip(request),
        )
        response = JSONResponse({
            "status": "ok",
            "user_id": data["user_id"],
            "role": data["role"],
        })
        set_auth_cookies(
            response,
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            csrf_token=data["csrf_token"],
        )
        return response


@router.post("/auth/logout")
def auth_logout(request: Request):
    with db_transaction() as (conn, _):
        revoke_current_refresh_session(conn, request=request)
        response = JSONResponse({"status": "ok"})
        clear_auth_cookies(response)
        return response


@router.get("/auth/csrf")
def auth_csrf(request: Request):
    csrf_cookie = request.cookies.get("csrf_token")
    return {
        "csrf_token": csrf_cookie or "",
        "has_csrf_cookie": bool(csrf_cookie),
    }
