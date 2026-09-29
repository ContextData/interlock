"""Security-admin workflows for pre-provisioning OIDC subject mappings."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from interlock.admin.audit import audit_admin_action, mutation_audit_detail

router = APIRouter(prefix="/api/admin-auth/oidc", tags=["oidc-mappings"])


class OIDCSubjectMapping(BaseModel):
    subject: str = Field(min_length=1, max_length=512)
    email: str | None = Field(default=None, max_length=320)

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("subject must not have surrounding whitespace or control characters")
        return value


async def _set_mapping(
    request: Request,
    *,
    principal_type: Literal["admin", "agent"],
    principal_id: int,
    mapping: OIDCSubjectMapping,
) -> dict[str, object]:
    pool = request.app.state.pg_pool
    try:
        if principal_type == "admin":
            row = await pool.fetchrow(
                """
                UPDATE admin_identities
                SET oidc_subject = $2, email = COALESCE($3, email), updated_at = NOW()
                WHERE id = $1
                RETURNING id, username AS name, oidc_subject, email,
                          authorization_version
                """,
                principal_id,
                mapping.subject,
                mapping.email,
            )
        else:
            row = await pool.fetchrow(
                """
                UPDATE identities
                SET oidc_subject = $2, updated_at = NOW()
                WHERE id = $1
                RETURNING id, name, oidc_subject
                """,
                principal_id,
                mapping.subject,
            )
    except Exception as exc:
        # Unique subject indexes intentionally prevent one subject from mapping
        # to multiple local principals. Do not expose database details.
        raise HTTPException(status_code=409, detail="OIDC subject is already mapped") from exc
    if row is None:
        raise HTTPException(status_code=404, detail=f"{principal_type} identity not found")
    result = dict(row)
    await audit_admin_action(
        request,
        action=f"oidc.{principal_type}_mapping.set",
        resource=f"{principal_type}_identity",
        resource_id=str(principal_id),
        success=True,
        strict=True,
        detail=mutation_audit_detail(
            after={
                "id": principal_id,
                "subject_configured": True,
                "email": result.get("email"),
            },
            changed_fields=(
                ("oidc_subject", "email") if principal_type == "admin" else ("oidc_subject",)
            ),
            status_code=200,
        ),
    )
    result["subject"] = result.pop("oidc_subject")
    return result


async def _delete_mapping(
    request: Request,
    *,
    principal_type: Literal["admin", "agent"],
    principal_id: int,
) -> dict[str, object]:
    pool = request.app.state.pg_pool
    table = "admin_identities" if principal_type == "admin" else "identities"
    row = await pool.fetchrow(
        f"""
        UPDATE {table}
        SET oidc_subject = NULL, updated_at = NOW()
        WHERE id = $1
        RETURNING id
        """,
        principal_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"{principal_type} identity not found")
    await audit_admin_action(
        request,
        action=f"oidc.{principal_type}_mapping.delete",
        resource=f"{principal_type}_identity",
        resource_id=str(principal_id),
        success=True,
        strict=True,
        detail=mutation_audit_detail(
            after={"id": principal_id, "subject_configured": False},
            changed_fields=("oidc_subject",),
            status_code=200,
        ),
    )
    return {"id": principal_id, "subject": None}


@router.put("/admins/{admin_id}")
async def set_admin_mapping(
    admin_id: int, body: OIDCSubjectMapping, request: Request
) -> dict[str, object]:
    return await _set_mapping(
        request,
        principal_type="admin",
        principal_id=admin_id,
        mapping=body,
    )


@router.delete("/admins/{admin_id}")
async def delete_admin_mapping(admin_id: int, request: Request) -> dict[str, object]:
    return await _delete_mapping(request, principal_type="admin", principal_id=admin_id)


@router.put("/agents/{identity_id}")
async def set_agent_mapping(
    identity_id: int, body: OIDCSubjectMapping, request: Request
) -> dict[str, object]:
    return await _set_mapping(
        request,
        principal_type="agent",
        principal_id=identity_id,
        mapping=body,
    )


@router.delete("/agents/{identity_id}")
async def delete_agent_mapping(identity_id: int, request: Request) -> dict[str, object]:
    return await _delete_mapping(request, principal_type="agent", principal_id=identity_id)
